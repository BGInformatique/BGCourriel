"""Courriel — tableau de bord des boîtes de courriel, pour Thunderbird sur Debian.

MODÈLE NEUTRE — voir PARAMETRES-A-CONFIGURER.md avant tout déploiement.

Ce que fait ce paquet, en une phrase : il lit les index de Thunderbird en
lecture seule, trie ce qu'il y trouve, et pousse un état dans Firestore
qu'une page web (à vous de la construire, ou de ne pas en avoir) affiche
en direct.

Les modules, dans l'ordre où les données les traversent :

    mork          lit le format Mork des fichiers .msf (l'index de Thunderbird)
    thunderbird   trouve le profil, les comptes et les dossiers
    courriels     transforme les lignes Mork en courriels propres
    classement    catégorie de chaque courriel, et file de ce qui attend un geste
    analyse       volumétrie, tendances, anomalies
    etat          assemble le document destiné à la page web
    firestore     l'envoie

LECTURE SEULE SUR LE COURRIER. Aucun module n'écrit dans le profil Thunderbird,
ne déplace un courriel, ne change un état de lecture ni ne se connecte à un
serveur de courrier. Les deux seuls fichiers que ce programme écrit sont son
journal de tendances (etat/historique.json) et son document Firestore.
"""

__version__ = "1.0.0"
