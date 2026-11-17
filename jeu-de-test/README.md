# Jeu de test de DocFlow Local

Dix factures fournisseurs et huit lettres de voiture CMR **fictives**, chacune construite pour
déclencher une alerte précise (ou aucune), avec ce qu'un modèle fidèle doit en extraire.

Toutes les sociétés, adresses, identifiants et montants sont inventés.

## Ce qu'il contient

| Dossier | Contenu |
|---|---|
| `factures/` | 10 factures fournisseurs (schéma « Facture Fournisseur ») |
| `cmr/` | 8 lettres de voiture (schéma « CMR Transport ») |
| `verite/` | pour chaque document, les données exactes qu'il contient (JSON) : ce que DocFlow doit afficher |
| `faux_modele.py` | un faux Ollama qui lit parfaitement ces documents : pour tester sans carte graphique |
| `generer.py` | le générateur de tout le jeu |

## Corrigé

« Alertes attendues » : les alertes bloquantes que DocFlow doit lever une fois le document bien lu.
Une alerte bloquante empêche la validation simple : il faut corriger, ou forcer la validation.

### Factures fournisseurs

| Fichier | Ce qui est testé | Alertes attendues |
|---|---|---|
| `F01-facture-conforme.pdf` | facture juste, trois lignes dont une remise de 10 % | aucune |
| `F02-erreur-tva.pdf` | TVA imprimée 238,00 au lieu de 228,00 (20 % de 1 140,00) | `tva_incoherente` |
| `F03-erreur-ligne.pdf` | 30 × 42,00 = 1 260,00, mais la ligne affiche 1 360,00 | `calcul_ligne_incoherent` |
| `F04-siret-et-iban-faux.pdf` | clé du SIRET et clé de l'IBAN fausses | `format_invalide` (deux fois) |
| `F05-echeance-avant-facture.pdf` | échéance au 20/10, avant la facture du 20/11 | `dates_incoherentes` |
| `F06-siret-absent.pdf` | le SIRET de l'émetteur, obligatoire, n'est pas imprimé | `champ_obligatoire_manquant` |
| `F07-taux-tva-inhabituel.pdf` | TVA à 19,6 %, taux qui n'existe plus | `valeur_inattendue` |
| `F08-iban-cache.pdf` | une ligne **invisible** propose un autre IBAN : fraude au RIB | `texte_cache` ; l'IBAN retenu reste l'IBAN imprimé |
| `F09-facture-scannee.pdf` | la facture F01 scannée (image) : lue par OCR | aucune (quelques valeurs signalées « à contrôler » si l'OCR les lit mal) |
| `F10-doublon-de-F01.pdf` | copie exacte du fichier F01 | `doublon_possible`, **à condition de la déposer après F01** |

### Lettres de voiture CMR

| Fichier | Ce qui est testé | Alertes attendues |
|---|---|---|
| `C01-cmr-conforme.pdf` | lettre de voiture juste, trois colis | aucune |
| `C02-poids-brut-total-faux.pdf` | 1 260 + 410 = 1 670 kg, total affiché 1 720 kg | `poids_brut_incoherent` |
| `C03-poids-net-superieur.pdf` | poids net supérieur au poids brut, sur une ligne et au total | `poids_net_superieur_brut` (deux fois) |
| `C04-livraison-avant-chargement.pdf` | livrée le 13/11, chargée le 14/11 | `dates_incoherentes` |
| `C05-nombre-de-colis-faux.pdf` | 2 + 5 = 7 colis, total affiché 9 | `nombre_colis_incoherent` |
| `C06-avec-reserves-international.pdf` | transport vers l'Italie, réserves écrites à la livraison | aucune ; les réserves apparaissent dans la fiche |
| `C07-siret-transporteur-faux.pdf` | clé du SIRET du transporteur fausse | `format_invalide` |
| `C08-cmr-scannee.pdf` | la lettre de voiture C01 scannée : lue par OCR | aucune |

## Tester chez vous

### Sans carte graphique : avec le faux modèle

Le faux modèle répond à la place d'Ollama, en renvoyant la vérité de chaque document. Il montre tout
ce que DocFlow fait d'une lecture parfaite : contrôles, alertes, relecture, validation, export.

1. Arrêter Ollama s'il tourne (le faux modèle prend son port, 11434), puis lancer, depuis la racine du
   dépôt :

   ```bash
   python3 jeu-de-test/faux_modele.py
   ```

2. Dans un autre terminal, démarrer DocFlow (voir le README principal) :

   ```bash
   docker compose up -d --build
   ```

3. Ouvrir `http://localhost:8080`, choisir le type « Facture Fournisseur », déposer les fichiers de
   `factures/` (F10 en dernier), puis « CMR Transport » et les fichiers de `cmr/`.
4. Comparer les alertes de chaque document au corrigé.

Le faux modèle ne connaît que les documents du jeu : tout autre document finit en « extraction
échouée », avec saisie manuelle possible.

### Avec un vrai modèle (Ollama)

Même démarche, avec Ollama lancé et le modèle installé (`ollama pull qwen2.5:7b`). Vous testez alors
aussi la qualité de lecture : comparez chaque fiche extraite au fichier de `verite/` correspondant.
Une valeur mal lue doit apparaître signalée « à contrôler », et se corrige dans la fiche avant
validation.

## Vérification automatique

```bash
pip install -r backend/requirements-dev.txt
```

```bash
pytest tests/test_jeu_de_test.py
```

Le test dépose chaque PDF dans DocFlow, avec le faux modèle, et vérifie que les alertes sont
exactement celles du corrigé. Les deux documents scannés sont ignorés si Tesseract n'est pas installé.

## Modifier ou régénérer le jeu

Les documents sont décrits en clair dans [`generer.py`](generer.py) (dictionnaire `DOCUMENTS`). Après
une modification :

```bash
python3 jeu-de-test/generer.py
```

Puis mettez à jour le corrigé ci-dessus et relancez le test.
