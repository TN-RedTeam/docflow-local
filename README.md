# DocFlow Local

Extraction, vérification et structuration automatiques de documents opérationnels
(lettres de voiture CMR, bons de livraison, factures fournisseurs, devis) pour les PME,
**sans jamais envoyer un fichier vers un cloud externe**.

Un PDF entre ; il en ressort des données structurées, contrôlées par un moteur
arithmétique déterministe, relues par un humain, puis exportées vers l'ERP ou la comptabilité.

```
PDF natif ou scanné
      │
      ▼
┌──────────────┐  texte   ┌───────────────────┐  JSON   ┌────────────────────────┐
│  Ingestion   │ ───────► │ LLM local Ollama  │ ──────► │ Vérification           │
│ PyMuPDF +    │          │ /v1/chat/         │         │ déterministe           │
│ pdfplumber + │          │ completions       │         │ (calculs, SIRET, dates,│
│ OCR Tesseract│          │ JSON Schema strict│         │  champs obligatoires)  │
└──────────────┘          └───────────────────┘         └───────────┬────────────┘
                                                                    │ validation_warnings
                                                                    ▼
┌──────────────┐  export  ┌───────────────────┐  HITL   ┌────────────────────────┐
│ ERP / compta │ ◄─────── │ PostgreSQL 16     │ ◄─────► │ Interface de révision  │
│ CSV · JSON   │          │ JSONB + audit +   │         │ PDF à gauche,          │
│ dossier dépôt│          │ verrou par trigger│         │ formulaire à droite    │
└──────────────┘          └───────────────────┘         └────────────────────────┘
```

## À quoi sert ce dépôt, et quand l'utiliser

**À quoi il sert.** DocFlow Local transforme des PDF (factures fournisseurs, lettres de voiture CMR) en
données vérifiées et exportables, avec un modèle d'IA qui tourne sur la machine du client.

**Utilisez-le quand :**

- une équipe ressaisit chaque jour des factures ou des documents de transport dans un logiciel ;
- les documents sont confidentiels et ne doivent pas partir vers un service en ligne ;
- vous voulez qu'un humain valide chaque fiche, avec les erreurs de calcul signalées ;
- vous craignez la fraude au faux RIB : le texte caché d'un PDF est détecté et écarté.

**Ne l'utilisez pas quand :**

- le besoin est une liaison directe avec un ERP : DocFlow exporte des fichiers, il ne s'y connecte pas ;
- il faut des comptes utilisateurs nominatifs : l'application n'a pas d'authentification, elle se place
  derrière le reverse-proxy de la box (`LLM-exploits/codesafe-ai-box`) ;
- le client n'a ni serveur ni PC capable de faire tourner un modèle local.

**Comment le lancer.** Seul, pour un essai : voir « Démarrage avec Docker ». Chez un client : par la box
CodeSafe, qui ajoute le mot de passe, le cloisonnement réseau et le filtre de sécurité.

### Les trois dépôts CodeSafe

| Dépôt | À quoi il sert | À utiliser quand |
|---|---|---|
| [`docflow-local`](https://github.com/TN-RedTeam/docflow-local) | Lire des factures et des documents de transport en PDF, contrôler les calculs, faire valider par un humain, exporter | une PME ressaisit des documents à la main et ne veut rien envoyer en ligne |
| [`TenderPulse`](https://github.com/TN-RedTeam/TenderPulse) | Collecter chaque jour les appels d'offres publics et les noter selon le profil d'une PME | une PME répond à des marchés publics et fait sa veille à la main |
| [`LLM-exploits`](https://github.com/TN-RedTeam/LLM-exploits) | Sécuriser, auditer et vendre : la box qui réunit les applications, l'outil d'audit, l'offre commerciale, et FreightGuard | vous installez chez un client, vous prouvez la sécurité, ou vous préparez un rendez-vous commercial |

## Sommaire

1. [Architecture](#architecture)
2. [Prérequis](#prérequis)
3. [Démarrage avec Docker](#démarrage-avec-docker)
4. [Tests](#tests)
5. [Utilisation](#utilisation)
6. [API REST](#api-rest)
7. [Contrôles et score de confiance](#contrôles-et-score-de-confiance)
8. [Ajouter un type de document](#ajouter-un-type-de-document)
9. [Configuration](#configuration)
10. [Développement sans Docker](#développement-sans-docker)
11. [Limites connues](#limites-connues)

## Architecture

```
docflow-local/
├── docker-compose.yml            db (PostgreSQL 16) + backend + frontend (+ ollama en option)
├── .env.example                  variables lues par docker compose
├── backend/
│   ├── Dockerfile                Python 3.12 + Tesseract (fra, eng)
│   ├── requirements.txt
│   ├── db/init.sql               schéma SQL complet, idempotent, avec triggers
│   ├── schemas/                  schémas documentaires pilotés par configuration
│   │   ├── transport_cmr.json
│   │   └── facture_fournisseur.json
│   └── app/
│       ├── main.py               fabrique FastAPI, file de traitement, reprise après arrêt
│       ├── config.py             paramètres + garde « LLM local uniquement »
│       ├── database.py, models.py
│       ├── schema_registry.py    JSON → JSON Schema strict + modèle Pydantic dynamique
│       ├── ingestion.py          texte natif, tableaux, repli OCR page par page
│       ├── llm_client.py         client Ollama (response_format json_schema strict)
│       ├── normalization.py      nombres et dates « à la française », SIRET, TVA, IBAN
│       ├── validation.py         moteur de cohérence + score de confiance
│       ├── pipeline.py           orchestration et journal d'audit
│       ├── exporter.py           CSV à plat, JSON, dépôt de fichiers pour l'ERP
│       └── routers/              endpoints REST
├── frontend/                     React 18 · TypeScript · Vite · Tailwind CSS
│   └── src/
│       ├── pages/Dashboard.tsx   import par lot, indicateurs, liste filtrable, export
│       ├── pages/Review.tsx      révision côte-à-côte et validation finale
│       └── components/DynamicForm.tsx   formulaire généré depuis le schéma
└── tests/
    ├── test_pipeline.py          test d'intégration : 2 vrais PDF générés, pipeline complet
    ├── test_validation.py        tests unitaires du moteur déterministe
    ├── sample_documents.py       génération des PDF d'essai (reportlab)
    └── fake_ollama.py            faux serveur Ollama pour les démonstrations sans GPU
```

Principes de conception :

- **Le LLM lit, il ne calcule pas.** Le modèle recopie ce qui est imprimé ; toute
  l'arithmétique (quantité × prix, sommes, TVA, TTC) est refaite en `Decimal` par du code
  Python. Une erreur du document *ou* du modèle produit la même alerte visible.
- **Un seul fichier JSON par type de document** génère le schéma imposé au LLM, le modèle
  Pydantic, le formulaire de révision et les colonnes d'export.
- **Rien ne se perd.** Si Ollama est arrêté ou si le PDF est illisible, le document est
  quand même enregistré, signalé, et peut être saisi à la main ou relancé.
- **Un document validé est immuable**, y compris hors API : un trigger PostgreSQL refuse
  toute modification ou suppression.

## Prérequis

| Composant | Version | Remarque |
|---|---|---|
| Docker + Docker Compose v2 | récent | pour le démarrage standard |
| [Ollama](https://ollama.com/download) | ≥ 0.5 | les sorties structurées JSON Schema sur `/v1/chat/completions` datent de la 0.5 |
| Modèle | `qwen2.5:7b` ou `llama3.2` | voir ci-dessous |
| Mémoire | 8 Go de RAM (16 Go conseillés) | un GPU de 8 Go accélère nettement `qwen2.5:7b` |

Installer le modèle sur la machine qui héberge Ollama :

```bash
ollama pull qwen2.5:7b
```

`qwen2.5:7b` (≈ 4,7 Go) est le choix recommandé : il respecte mieux les tableaux et les
identifiants. `llama3.2` (3 milliards de paramètres, ≈ 2 Go) convient aux machines sans GPU,
avec davantage de corrections manuelles à prévoir sur les documents denses.

```bash
ollama pull llama3.2
```

Deux réglages Ollama comptent en pratique :

1. **Écoute réseau.** Depuis des conteneurs Docker, Ollama doit écouter au-delà de
   `127.0.0.1`. Sous Linux : `sudo systemctl edit ollama`, ajouter
   `Environment="OLLAMA_HOST=0.0.0.0"` dans la section `[Service]`, puis
   `sudo systemctl restart ollama`. Protégez alors le port 11434 par le pare-feu.
2. **Fenêtre de contexte.** L'endpoint compatible OpenAI ne permet pas de fixer `num_ctx`
   par requête. Pour les documents de plusieurs pages, augmentez-la côté serveur avec
   `Environment="OLLAMA_CONTEXT_LENGTH=8192"` (même fichier), sinon le texte est tronqué
   silencieusement par Ollama.

## Démarrage avec Docker

```bash
cp .env.example .env
```

Modifiez au minimum `POSTGRES_PASSWORD` dans `.env`, puis :

```bash
docker compose up -d --build
```

- Interface : <http://localhost:8080>
- API et documentation interactive : <http://localhost:8000/docs> (liée à `127.0.0.1`)
- État du service : <http://localhost:8000/api/health>

Variante avec Ollama en conteneur (au lieu d'une installation sur l'hôte) : mettez
`OLLAMA_BASE_URL=http://ollama:11434` dans `.env`, puis :

```bash
docker compose --profile ollama up -d --build
```

```bash
docker compose exec ollama ollama pull qwen2.5:7b
```

Arrêt (les données PostgreSQL sont conservées dans le volume `pgdata`) :

```bash
docker compose down
```

## Tests

```bash
pip install -r backend/requirements-dev.txt
```

```bash
python -m pytest
```

`tests/test_pipeline.py` génère deux vrais fichiers PDF avec reportlab :

- un **bon de livraison / CMR valide** (3 colis, poids et totaux cohérents) ;
- une **facture fournisseur avec une erreur volontaire de TVA** : 1 000,00 € HT à 20 %,
  mais 210,00 € de TVA imprimés au lieu de 200,00 €.

Chaque PDF traverse la chaîne réelle : import HTTP, extraction du texte, requête du client
Ollama, normalisation, contrôles, persistance, correction humaine, validation, export CSV et
JSON. Seule la réponse HTTP d'Ollama est simulée (`httpx.MockTransport`) ; le test vérifie
au passage que la requête envoyée contient bien le texte du PDF et un `response_format`
JSON Schema strict. Résultat attendu : le CMR passe sans alerte, la facture lève
`tva_incoherente` sur le champ `montant_tva` (attendu 200,00, lu 210,00).

Par défaut les tests utilisent une base SQLite jetable. Pour les exécuter sur PostgreSQL,
trigger de verrouillage compris :

```bash
TEST_DATABASE_URL=postgresql+psycopg://docflow:docflow@localhost:5432/docflow_test python -m pytest
```

Attention : cette commande vide les tables `processed_documents` et `extraction_audit_logs`
de la base indiquée. Utilisez une base dédiée aux tests.

Pour rejouer le scénario de la facture avec un **vrai modèle local** :

```bash
DOCFLOW_LIVE_OLLAMA=1 OLLAMA_MODEL=qwen2.5:7b python -m pytest tests/test_pipeline.py -k live
```

Le test OCR est ignoré automatiquement si Tesseract n'est pas installé sur la machine.

## Utilisation

1. **Tableau de bord** — choisissez le type de document (CMR Transport ou Facture
   Fournisseur) puis glissez-déposez un lot de PDF. L'extraction tourne en tâche de fond ;
   la liste se met à jour seule.
2. **Révision** — le PDF est affiché à gauche, le formulaire généré depuis le schéma à
   droite. Les champs en défaut sont encadrés :
   - **rouge** : champ obligatoire absent, identifiant ou date invalide ;
   - **ambre** : incohérence de calcul, avec le détail du calcul et un bouton pour appliquer
     la valeur attendue ;
   - **bleu pointillé** : valeur introuvable telle quelle dans le texte du document
     (à contrôler à l'œil, non bloquant).

   Chaque correction est renvoyée au moteur de vérification : les alertes reflètent toujours
   les données saisies.
3. **Validation** — « Valider et exporter » verrouille l'enregistrement en base, dépose le
   JSON du document dans le dossier d'export du serveur et télécharge l'export (CSV ou JSON)
   dans le navigateur. S'il reste des alertes, l'opérateur doit confirmer explicitement ;
   sa décision est inscrite dans le journal d'audit.

Pour essayer l'application sans GPU ni modèle, un faux serveur Ollama renvoie les réponses
correspondant aux deux PDF d'exemple (il ne lit pas réellement les documents) :

```bash
python tests/fake_ollama.py --write-samples ./exemples
```

```bash
python tests/fake_ollama.py --port 11434
```

## API REST

| Méthode | Route | Rôle |
|---|---|---|
| `POST` | `/api/documents/upload` | Import unitaire ou par lot (`multipart/form-data` : `schema_code`, `files`). `?wait=true` attend la fin de l'extraction. |
| `GET` | `/api/documents` | Liste filtrable : `schema`, `status`, `q`, `has_warnings`, `date_from`, `date_to`, `limit`, `offset`. |
| `GET` | `/api/documents/stats` | Compteurs par statut. |
| `GET` | `/api/documents/export?format=csv\|json` | Export normalisé des documents validés. Filtres : `schema`, `ids`, `only_new`, `date_from`, `date_to`, `status`. |
| `GET` | `/api/documents/{id}` | Données extraites, alertes, score. |
| `GET` | `/api/documents/{id}/file` | PDF source. |
| `GET` | `/api/documents/{id}/audit` | Journal d'audit du document. |
| `PATCH` | `/api/documents/{id}` | Enregistre une correction et recalcule les alertes, sans verrouiller. |
| `PATCH` | `/api/documents/{id}/validate` | Validation finale après correction humaine (`data`, `actor`, `force`). Verrouille et déclenche l'export. |
| `POST` | `/api/documents/{id}/reprocess` | Relance l'extraction. |
| `DELETE` | `/api/documents/{id}` | Supprime un document non validé. |
| `GET` | `/api/schemas` | Schémas documentaires disponibles. |
| `GET` | `/api/health` | État de la base, d'Ollama et du modèle. |

Exemple en ligne de commande :

```bash
curl -F schema_code=facture_fournisseur -F files=@facture.pdf "http://localhost:8000/api/documents/upload?wait=true"
```

```bash
curl -o factures.csv "http://localhost:8000/api/documents/export?format=csv&schema=facture_fournisseur&only_new=true"
```

**Format CSV** : UTF-8 avec BOM, séparateur `;`, point décimal, dates ISO. Une ligne par
article (ou par colis), les champs d'en-tête étant répétés ; colonnes nommées par chemin
(`emetteur.siret`, `lignes.montant_ht`). Le CSV porte sur un seul type de document.

**Dépôt de fichiers** : à chaque validation, le backend écrit
`<EXPORT_DIR>/<type>/<id>.json` de façon atomique. Avec Docker, ces fichiers sont dans le
volume `exports`. Pour les exposer à un ERP, remplacez dans `docker-compose.yml`
`exports:/data/exports` par un dossier de l'hôte (`./exports:/data/exports`) accessible en
écriture à l'utilisateur d'UID 10001.

## Contrôles et score de confiance

Statuts d'un document : `processing` → `pending_review` → `validated`.

Chaque alerte de `validation_warnings` a la forme :

```json
{
  "code": "tva_incoherente",
  "severity": "warning",
  "field": "montant_tva",
  "message": "Montant de TVA ≠ total HT × taux de TVA : attendu 200,00, lu 210,00 (écart 10,00). Calcul : 1000,00 × 20,00 %.",
  "expected": 200.0,
  "actual": 210.0
}
```

| Contrôle | Code | Schéma |
|---|---|---|
| Champ obligatoire absent, tableau vide | `champ_obligatoire_manquant` | tous |
| SIRET / SIREN (Luhn), TVA intracommunautaire (clé), IBAN (mod 97), code postal, date | `format_invalide` | tous |
| Valeur hors liste (taux de TVA : 0 ; 2,1 ; 5,5 ; 10 ; 20) | `valeur_inattendue` | facture |
| Montant HT de ligne ≠ quantité × PU HT − remise | `calcul_ligne_incoherent` | facture |
| Total HT ≠ somme des lignes | `total_ht_incoherent` | facture |
| TVA ≠ total HT × taux | `tva_incoherente` | facture |
| TTC ≠ HT + TVA | `ttc_incoherent` | facture |
| Poids brut / net total ≠ somme des colis ; nombre de colis | `poids_brut_incoherent`, `poids_net_incoherent`, `nombre_colis_incoherent` | CMR |
| Poids net > poids brut | `poids_net_superieur_brut` | CMR |
| Dates dans le désordre | `dates_incoherentes` | tous |
| Fichier déjà importé (même empreinte SHA-256) | `doublon_possible` | tous |
| Valeur introuvable dans le texte source | `valeur_non_retrouvee` (info) | tous |
| Ollama injoignable, PDF illisible | `extraction_echouee`, `texte_illisible` | tous |

Tolérance d'arrondi : 0,02 € par défaut (`AMOUNT_TOLERANCE`), ajustable règle par règle.

**Score de confiance** (0 à 1), volontairement simple et explicable : moyenne de la
complétude des champs obligatoires et de la part des valeurs retrouvées dans le texte
source, multipliée par 0,85 pour chaque incohérence et 0,70 pour chaque erreur, puis par la
qualité moyenne de l'OCR si le document est scanné. Ce n'est pas une probabilité issue du
modèle : c'est un indicateur de tri pour savoir quels documents regarder en premier.

### Texte caché dans les PDF

Avant toute extraction, chaque page est inspectée pour repérer le texte qu'un lecteur humain ne voit
pas mais qu'un extracteur lit : mode de rendu invisible, corps inférieur à 1 point, texte transparent,
texte placé hors de la page, texte de la couleur de son fond (blanc sur blanc). Ce texte est **retiré**
de ce qui est transmis au modèle, et le document reçoit l'alerte bloquante `texte_cache`, qui survit aux
corrections : seule une validation forcée, tracée dans l'audit, peut passer outre. La couche texte
invisible d'un scan n'est pas crue sur parole : la page est relue par OCR.

Limite : un texte masqué par une image posée par-dessus, ou écrit en très petit mais au-dessus de
1 point, n'est pas détecté.

## Ajouter un type de document

Déposez un fichier `backend/schemas/<code>.json` et redémarrez le backend : le schéma est
validé, synchronisé dans la table `document_schemas` et apparaît dans l'interface.

```json
{
  "code": "devis",
  "label": "Devis",
  "description": "Devis fournisseur",
  "fields": [
    { "name": "numero_devis", "label": "Numéro de devis", "type": "string", "required": true },
    { "name": "date_validite", "label": "Valable jusqu'au", "type": "date" },
    {
      "name": "lignes", "label": "Lignes", "type": "array", "min_items": 1,
      "fields": [
        { "name": "designation", "label": "Désignation", "type": "string", "required": true },
        { "name": "quantite", "label": "Quantité", "type": "number" },
        { "name": "prix_unitaire_ht", "label": "PU HT", "type": "number", "unit": "€" },
        { "name": "montant_ht", "label": "Montant HT", "type": "number", "unit": "€" }
      ]
    },
    { "name": "total_ht", "label": "Total HT", "type": "number", "required": true, "unit": "€" }
  ],
  "validation_rules": [
    { "type": "product_equals", "code": "calcul_ligne_incoherent", "label": "Montant ≠ quantité × PU",
      "array": "lignes", "factors": ["quantite", "prix_unitaire_ht"], "result": "montant_ht" },
    { "type": "sum_array_equals", "code": "total_ht_incoherent", "label": "Total HT ≠ somme des lignes",
      "array": "lignes", "item_field": "montant_ht", "total": "total_ht" }
  ],
  "export": { "line_array": "lignes" }
}
```

- Types de champ : `string`, `text`, `number`, `integer`, `date`, `boolean`, `object`, `array`
  (tableau d'objets à champs scalaires).
- Formats : `siret`, `siren`, `tva_intracom`, `iban`, `code_postal_fr`.
- Règles : `product_equals`, `sum_array_equals`, `sum_fields_equals`, `percentage_equals`,
  `less_or_equal`. Une règle qui référence un champ inexistant empêche le démarrage, avec un
  message explicite.

## Configuration

Variables d'environnement du backend :

| Variable | Défaut | Rôle |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://docflow:docflow@localhost:5432/docflow` | Connexion PostgreSQL |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Instance Ollama |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Modèle d'extraction |
| `OLLAMA_TIMEOUT_SECONDS` | `300` | Délai maximal par document |
| `OLLAMA_MAX_RETRIES` | `2` | Nouvelles tentatives si la réponse est inexploitable |
| `ALLOW_REMOTE_LLM` | `false` | Voir ci-dessous |
| `LLM_MAX_INPUT_CHARS` | `24000` | Longueur maximale du texte transmis au modèle |
| `OCR_LANGUAGES` | `fra+eng` | Langues Tesseract |
| `OCR_DPI` | `300` | Résolution de rendu avant OCR |
| `MAX_UPLOAD_MB` / `MAX_BATCH_FILES` | `25` / `50` | Limites d'import |
| `WORKER_CONCURRENCY` | `1` | Documents traités en parallèle (1 suffit pour un seul GPU) |
| `EXPORT_DIR` | vide | Dossier de dépôt des JSON validés (désactivé si vide) |
| `AMOUNT_TOLERANCE` | `0.02` | Tolérance d'arrondi des contrôles |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost:8080` | Origines autorisées |

**Garde de confidentialité** : le backend refuse de démarrer si `OLLAMA_BASE_URL` ne désigne
pas la machine locale, une adresse IP privée ou un nom d'hôte interne. Cela évite de
brancher par inadvertance une API cloud. `ALLOW_REMOTE_LLM=true` lève cette garde, à vos
risques.

## Développement sans Docker

Backend (PostgreSQL 16 et Tesseract installés localement) :

```bash
cd backend && pip install -r requirements-dev.txt
```

```bash
cd backend && DATABASE_URL=postgresql+psycopg://docflow:docflow@localhost:5432/docflow uvicorn app.main:app_factory --factory --reload --port 8000
```

Frontend (relaie `/api` vers `http://localhost:8000`) :

```bash
cd frontend && npm install
```

```bash
cd frontend && npm run dev
```

Le backend applique lui-même `backend/db/init.sql` au démarrage : aucune migration manuelle
n'est nécessaire sur une base vide.

## Limites connues

- **Pas d'authentification.** L'application est conçue pour un poste ou un réseau local de
  confiance. Le port 8080 est exposé sur le réseau : placez-le derrière un reverse proxy
  avec authentification, ou restreignez-le par pare-feu, avant tout usage multi-postes. Le
  nom d'opérateur inscrit dans l'audit est déclaratif.
- **La qualité d'extraction dépend du modèle et du document.** Les contrôles déterministes
  détectent les incohérences de calcul et de format, pas une raison sociale mal lue ou une
  ligne oubliée dont le total resterait cohérent. La relecture humaine reste nécessaire ;
  l'alerte « valeur introuvable dans le texte » aide à repérer les inventions du modèle.
- **Factures à plusieurs taux de TVA** : le schéma fourni porte un taux unique. Sur une
  facture multi-taux, laissez le taux vide (le contrôle de TVA est alors sauté) ou ajoutez
  une ventilation au schéma.
- **Remises globales, frais de port hors lignes** : ils déclenchent l'alerte
  `total_ht_incoherent`, à valider en connaissance de cause.
- **Écriture manuscrite** : Tesseract lit mal les réserves manuscrites des CMR ; elles sont
  à saisir à la relecture.
- **Visionneuse PDF** : l'écran de révision s'appuie sur le lecteur PDF intégré du
  navigateur (Chrome, Edge, Firefox, Safari). Un lien « ouvrir dans un nouvel onglet » sert
  de repli.
- **Fichiers source stockés en base** (`BYTEA`) : simple à sauvegarder avec `pg_dump`,
  adapté à quelques dizaines de milliers de documents ; au-delà, prévoir un stockage fichier.
