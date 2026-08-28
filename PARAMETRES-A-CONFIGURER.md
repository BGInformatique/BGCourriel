# Paramètres à configurer

Ce dossier est une copie neutre de l'outil de triage de courriel utilisé en
production chez BG Informatique. Le code est déjà générique — aucune règle
métier n'est propre à une entreprise en particulier — mais quelques valeurs
restent à fournir avant de le déployer pour un autre client.

## 1. Chemin d'installation

Les fichiers `systemd/bg-courriel.service` et `LISEZ-MOI.md` contiennent le
jeton `{{CHEMIN_INSTALLATION}}`. Remplacez-le par le chemin absolu où vous
copiez ce dossier, par exemple `/home/<utilisateur>/Outils/Courriel`.

Si ce chemin contient une espace, gardez les guillemets déjà en place dans
le `.service` — voir le commentaire en tête du fichier pour pourquoi.

## 2. Configuration Firestore

Deux façons de faire, au choix :

- **Indépendant** : créez `~/.config/bg-courriel/config.json` :

  ```json
  {"projet": "<id-projet-firebase>", "uid": "<uid-du-compte-proprietaire>",
   "cle_sa": "/chemin/vers/cle-compte-service.json"}
  ```

- **Partagé avec un lanceur existant** : si vous déployez aussi un outil qui
  écrit déjà dans `~/.config/bg-lanceur/config.json`, ce programme l'emprunte
  automatiquement (voir `bg_courriel/firestore.py`, fonction `charger_config`)
  — rien à faire de plus.

Le compte de service n'a besoin d'écrire qu'un seul document :
`users/<uid>/courriel/etat`. Écrivez les règles Firestore en conséquence
avant de brancher une page web dessus.

## 3. Nom des services systemd

Les unités s'appellent `bg-courriel.service` / `bg-courriel.timer`. Ça reste
fonctionnel tel quel — le préfixe `bg-` n'est qu'une convention de nommage
héritée de BG Informatique. Renommez-les si vous préférez une autre
convention (par ex. `courriel.service`), en gardant les noms cohérents entre
les deux fichiers `.service`/`.timer` et vos commandes `systemctl`.

## 4. Ce qui n'est PAS inclus

- **La page web** qui affiche le document Firestore en direct — c'est un
  projet HTML/JS séparé, à construire vous-même (ou à sauter : le document
  Firestore est utilisable tel quel par n'importe quel client).
- **Le compte de service Google Cloud** et sa clé — à créer dans votre propre
  projet Firebase.

## 5. Ce qui n'a PAS besoin d'être touché

Les règles de classement (`bg_courriel/classement.py`) sont déjà génériques :
catégories, motifs et domaines couvrent des cas universels (formulaires de
site, factures, alertes techniques, infolettres). Les seuls domaines
explicitement listés (Telus, Vidéotron, Hydro-Québec, Interac…) sont des
fournisseurs québécois — à adapter si votre client n'est pas au Québec, sinon
laissez tel quel.
