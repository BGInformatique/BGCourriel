# Courriel — tableau de bord des boîtes de courriel

**MODÈLE NEUTRE.** Voir `PARAMETRES-A-CONFIGURER.md` avant tout déploiement —
ce fichier liste tout ce qui doit être adapté à votre entreprise avant de
lancer l'outil.

Lit les boîtes de Thunderbird sur la machine où il tourne, trie ce qu'il y
trouve, et pousse un état dans Firestore qu'une page web à vous (pas fournie
ici) peut afficher en direct.

**Aucun courriel n'est jamais touché.** Ni déplacé, ni marqué, ni supprimé.
L'outil n'ouvre les fichiers qu'en lecture, ne se connecte à aucun serveur de
courrier et ne demande aucun mot de passe. Les deux seuls fichiers qu'il écrit
sont son journal de tendances (`etat/historique.json`) et son document Firestore.

## Démarrer

```bash
cd {{CHEMIN_INSTALLATION}}

python3 -m bg_courriel dossiers     # ce que l'outil voit
python3 -m bg_courriel collecter    # lit et trie, N'ENVOIE RIEN
python3 -m bg_courriel pousser      # lit, trie et envoie
python3 -m bg_courriel diagnostic   # tout ce qui sert à comprendre une panne
python3 -m bg_courriel selftest     # vérifie le lecteur et le tri
```

En cas de doute, `collecter` d'abord : il montre exactement ce qui partirait
sans rien écrire nulle part.

Aucune dépendance à installer. Python 3 de Debian et la commande `openssl`
suffisent — pas d'environnement virtuel à entretenir, rien à mettre à jour.

## Ce que l'outil lit, et pourquoi ce n'est pas le mbox

Thunderbird garde deux copies de chaque boîte : le **mbox** (le texte des
courriels) et le **.msf** (son index, au format Mork). L'outil lit le .msf.

Ce n'est pas un détail d'optimisation, c'est une question de justesse : sur un
dossier IMAP, l'octet d'état de lecture du mbox est écrit au téléchargement et
Thunderbird ne le met jamais à jour. Mesuré en production sur une vraie boîte :
les courriels du mbox s'annonçaient **tous lus** alors qu'une bonne partie
étaient **non lus**. Un tableau de bord bâti sur le mbox afficherait
« rien à traiter » sur une boîte pleine.

Le .msf porte en plus l'objet, l'expéditeur, la date, la taille, le fil et un
aperçu du corps — dans 178 Ko au lieu de 30 Mo. Le mbox n'est donc jamais ouvert.

### L'autotest ne contient aucun chiffre codé en dur

Thunderbird écrit lui-même, dans son index, le nombre de courriels du dossier et
le nombre de non lus. `selftest` compare NOTRE comptage à CES compteurs. Cet
oracle se met à jour tout seul : il reste valable quand une boîte est ajoutée,
quand du courrier arrive, quand tout est lu. Un test qui vérifierait « 305 »
serait faux au prochain courriel.

### Quels dossiers sont lus

`INBOX`, `Archive`, et tout dossier que vous créez — c'est-à-dire le courrier
REÇU. Sont écartés : envoyés, brouillons, boîte d'envoi (ce n'est pas du courrier
reçu, et un brouillon porte le drapeau « non lu », donc il entrerait dans la file
d'action), corbeille et indésirables (déjà jugés), et les dossiers non postaux
qu'Exchange expose en IMAP — calendrier, contacts, tâches, notes, conflits de
synchronisation.

Les noms sont comparés après décodage : Thunderbird nomme ses fichiers comme le
serveur, donc en UTF-7 modifié — « Éléments envoyés » s'écrit
`&AMk-l&AOk-ments envoy&AOk-s` sur le disque. La première version comparait les
noms bruts, aucune exclusion n'accrochait, et le jour où Thunderbird a
synchronisé les 35 dossiers réels d'Office 365, le total est passé de 305 à 342
courriels sans qu'aucun courriel ne soit arrivé.

`python3 -m bg_courriel dossiers` liste ce qui est lu et rappelle ce qui est
écarté.

### Le décalage, dit franchement

Thunderbird garde son index en mémoire et ne l'écrit sur disque que de temps à
autre. Quand il est ouvert, les chiffres peuvent retarder de quelques minutes
sur ce que montre son écran. C'est le prix de ne dépendre d'aucun mot de passe
et d'aucune extension. La page web affiche donc DEUX âges : celui de la relève
et celui de l'index — la colonne « index lu » du tableau des boîtes.

## Le tri

Deux mécaniques distinctes, qui répondent à deux questions différentes.

### La catégorie — « de quoi s'agit-il ? »

Décidée par des règles sur l'expéditeur et l'objet, dans l'ordre du fichier, la
première qui accroche gagne.

| Catégorie | Ce qu'elle contient |
|---|---|
| `demande` | un client ou un prospect écrit — formulaires du site inclus |
| `affaires` | un échange avec une personne réelle |
| `facture` | facture, reçu, paiement, abonnement |
| `technique` | alertes des outils : GitHub, hébergement, sécurité |
| `sauvegarde` | notification d'un produit de sauvegarde (Macrium, Retrospect…) |
| `emploi` | postulations, recruteurs, offres |
| `infolettre` | courrier de masse |
| `autre` | rien n'a accroché |

Pour voir le classement obtenu, expéditeur par expéditeur :

```bash
python3 -m bg_courriel regles
```

Pour le modifier, écrire le fichier de règles et l'éditer — aucun redémarrage,
la relève suivante le lit :

```bash
python3 -m bg_courriel regles --ecrire   # crée regles.json
```

Un fichier illisible ne fait pas tomber la relève : il produit une plainte,
visible dans le diagnostic ET sur la page web, et l'outil continue avec ses
règles de départ.

### Le cas particulier de « sauvegarde »

Reconnue comme les autres — une règle sur l'objet (`Macrium`, `Retrospect`,
`ProActive`, `SQL Server Job System`, `vzdump`…) — mais elle seule déclenche
une seconde passe : `sauvegardes.py` lit le corps COMPLET du courriel (pas
l'aperçu tronqué du .msf, insuffisant pour un motif d'erreur enfoui plus bas
dans un rapport) via `corps.py`, qui va chercher ce texte À LA DEMANDE dans le
mbox, au byte exact que donne `storeToken` — jamais une lecture du mbox
entier, et jamais pour les courriels des autres catégories.

Cette seconde passe décide erreur / avertissement / succès / inconnu, la
machine, la cause et l'action conseillée — le moteur reprend l'analyse d'un
outil antérieur de vérification de sauvegardes, adapté ici au triage.
Une erreur ou un avertissement entre dans la file d'action **même si le
courriel a déjà été lu** : contrairement au reste de l'outil, un échec de
sauvegarde n'est pas réglé parce qu'on l'a ouvert.

Ce qui n'a PAS été repris : le suivi d'une tâche dans le temps (backup
manquant, épisodes récurrents sur 90 jours) — ça demandait une mémoire locale
que cet outil n'a pas (un seul document Firestore, réécrit en entier à
chaque relève). Ce module répond à « ce courriel-ci est-il en échec ? », pas
à « quelle tâche n'a plus donné signe de vie ? ».

**Ce qu'il faut savoir avant d'y toucher.** L'ordre des règles est le mécanisme,
pas un hasard. Trois pièges y sont déjà désarmés, chacun découvert sur les vraies
boîtes et commenté dans `classement.py` :

- `submissions@formspree.io` est une demande de client ; `newsletter@formspree.io`
  est du bruit. Même domaine, sens opposés : les adresses exactes passent avant
  les domaines.
- Un dépôt de code peut s'appeler quelque chose comme **Facturation**, et son nom
  voyage dans l'objet de chaque échec de test. Le motif `factur` y accrochait :
  une quarantaine d'échecs de CI se rangeaient dans « factures ». Une règle
  étroite sur la forme `[propriétaire/dépôt]` passe donc avant les mots
  d'argent.
- Coursera écrit depuis cinq sous-domaines. Les domaines sont comparés par
  suffixe, pas à l'identique, sinon il faudrait courir après chaque nouveau.

### La file d'action — « qu'est-ce qui attend quelque chose de moi ? »

Décidée non par des règles mais par l'état du courriel.

| Raison | Quand |
|---|---|
| `marqué` | marqué à la main, sans réponse |
| `sans réponse` | un humain attend depuis plus que le seuil (3 jours par défaut) |
| `à répondre` | un humain attend, mais depuis peu |
| `à voir` | non lu, et sa catégorie mérite encore un regard |

Un courriel sort de la file dès qu'une réponse est partie. Le dossier
« Envoyés » n'est pas synchronisé sur cette machine, mais ce n'est pas
nécessaire : Thunderbird pose un drapeau « répondu » sur le courriel d'origine,
et ce drapeau vient du serveur IMAP — il est donc juste même si la réponse est
partie du téléphone.

**Le non-lu se périme, et c'est délibéré.** Une alerte d'outil non lue depuis
quinze jours sort de la file ; une offre d'emploi, après trente jours ; un
courriel d'humain LU et laissé sans réponse, après quatre-vingt-dix. Une facture
ne se périme jamais — elle peut être impayée. Le même courriel jamais OUVERT
reste dans la file à tout âge.

Pourquoi ces bornes existent : la première version mettait dans la file tout
non-lu et tout courriel sans réponse. Résultat mesuré, **337 entrées sur 426
courriels** — une file qui contient 79 % de la boîte ne trie rien, elle recopie
la boîte en changeant son titre. Rien n'est caché pour autant : ce qui sort de
la file est compté sous « en retard », sur la page.

### Les anomalies

Comparées à l'habitude de chaque boîte, jamais à un seuil fixe — la médiane de
la boîte elle-même sert de référence, parce que vingt courriels par jour est
beaucoup pour une boîte et peu pour une autre.

- **afflux** : beaucoup plus de courrier aujourd'hui que d'ordinaire ;
- **silence** : rien reçu alors qu'il en arrive habituellement ;
- **expéditeur muet** : quelqu'un d'habituellement régulier s'est arrêté.

L'outil signale ce qu'il a mesuré ; il ne conclut pas. Un silence peut être une
panne de réception, un congé ou un client parti.

## Firestore

Un seul document, réécrit en entier à chaque relève :

```
users/<uid>/courriel/etat
```

Par défaut, la configuration est empruntée à celle d'un lanceur déjà en place
(`~/.config/bg-lanceur/config.json` et sa clé de compte de service), **si vous
utilisez aussi cet outil-là** — projet, identifiant du propriétaire et clé
sont alors les mêmes, pour éviter que les deux divergent. Sinon, créer
directement `~/.config/bg-courriel/config.json` avec `projet`, `uid` et
`cle_sa` (voir `PARAMETRES-A-CONFIGURER.md`).

**Le document contient des objets et des adresses en clair**, y compris de
clients. Ce qui le protège :

- des règles Firestore à écrire vous-même, qui ne laissent lire
  `users/<uid>/courriel/` qu'au compte du propriétaire ;
- **l'écriture est refusée au navigateur** — la page n'écrit rien, et un jeton
  volé ne peut donc pas fabriquer une fausse file d'action vide ;
- le seul écrivain est le compte de service, depuis cette machine.

La limite d'un mégaoctet par document est une contrainte dure. La file d'action
porte le détail complet, la liste des courriels récents est bornée, et **tout ce
qui est écarté est compté et affiché** (champ `coupes`). Sur les boîtes
actuelles, le document pèse environ 270 Ko sur les 1024 permis.

## Minuterie

```bash
systemctl --user status bg-courriel.timer     # état
systemctl --user start bg-courriel.service    # relève tout de suite
journalctl --user -u bg-courriel.service -n 40
```

La relève tourne toutes les dix minutes. Elle ne dépend pas de Thunderbird :
qu'il soit ouvert ou fermé, l'index est là.

## Où sont les choses

| Fichier | Rôle |
|---|---|
| `bg_courriel/mork.py` | lit le format Mork des `.msf` |
| `bg_courriel/thunderbird.py` | profil, comptes, dossiers |
| `bg_courriel/courriels.py` | lignes Mork → courriels propres |
| `bg_courriel/classement.py` | catégories et file d'action |
| `bg_courriel/corps.py` | corps complet d'UN courriel, à la demande (mbox + storeToken) |
| `bg_courriel/sauvegardes.py` | statut d'une notification de sauvegarde — moteur repris de backup-monitor |
| `bg_courriel/analyse.py` | volumétrie, tendances, anomalies |
| `bg_courriel/etat.py` | assemble le document web |
| `bg_courriel/firestore.py` | l'envoie |
| `bg_courriel/selftest.py` | 126 vérifications |
| `regles.json` | vos règles, si vous en écrivez |
| `etat/historique.json` | une empreinte par relève, pour les courbes |

La page web qui affiche le document Firestore n'est **pas fournie** dans ce
modèle — c'est un petit projet à part (HTML/JS statique + règles Firestore).
À construire, ou à sauter si le document Firestore suffit tel quel.
