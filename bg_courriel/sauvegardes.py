"""Détection des courriels de sauvegarde : produit, statut (erreur /
avertissement / succès), cause et action conseillée.

REPRIS TEL QUEL DE backup-monitor (BGBackupChecker), retiré le 2026-08-19 au
profit de ce module : chaque motif ici encode un vrai incident déjà vu sur un
parc réel de clients (les commentaires le racontent). Rien n'est réinventé —
seule l'enveloppe change (RawMail/BackupEvent restent des objets locaux, sans
dépendance à un config.yaml : aucune section « parsers » à fusionner ici).

CE QUI N'EST PAS REPRIS, ET POURQUOI. backup-monitor suivait aussi l'HISTORIQUE
d'une tâche dans le temps (historique.json) pour détecter un backup MANQUANT
(aucun courriel reçu dans la fenêtre attendue) et regrouper les épisodes d'un
même problème sur 90 jours. BGCourriel n'a pas cette mémoire : chaque relève
réécrit un seul document Firestore d'un coup (voir etat.py). Ce module ne
répond donc qu'à « ce courriel de sauvegarde est-il en échec ? », pas à
« quelle tâche n'a plus donné signe de vie ? ».

Le corps complet du courriel est nécessaire ici — l'aperçu tronqué du .msf ne
suffit pas à retrouver un motif d'erreur enfoui plus bas dans un rapport
Macrium ou Retrospect. Voir corps.py : le corps n'est lu que pour les
courriels déjà repérés « sauvegarde » par classement.py (règle sur l'objet),
jamais pour le reste de la boîte.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from . import corps

STATUS_ERROR = "erreur"
STATUS_WARNING = "avertissement"
STATUS_SUCCESS = "succes"
STATUS_UNKNOWN = "inconnu"


@dataclass
class RawMail:
    """Courriel brut à classer — mêmes champs que backup-monitor, remplis
    depuis un courriel BGCourriel + son corps complet (voir corps.py)."""
    subject: str
    sender: str
    received: datetime
    body: str
    folder: str
    product: str
    client: str = ""
    attachments_text: str = ""
    attachments_note: str = ""


@dataclass
class BackupEvent:
    """Résultat du classement d'un courriel de sauvegarde."""
    product: str
    status: str
    subject: str
    sender: str
    received: datetime
    folder: str
    machine: str = ""
    job: str = ""
    matched_pattern: str = ""
    excerpt: str = ""
    problem: str = ""
    problem_key: str = ""
    problem_label: str = ""
    attachments_note: str = ""
    client: str = ""


class _TableFold(dict):
    """Table de pliage paresseuse pour str.translate (NFD, sans accents,
    minuscules) — identique à celle de backup-monitor."""

    def __missing__(self, cp: int) -> str:
        v = "".join(c.lower()
                    for c in unicodedata.normalize("NFD", chr(cp))
                    if not unicodedata.combining(c))
        self[cp] = v
        return v


_FOLD = _TableFold()


def fold_text(s: str) -> str:
    """Minuscules sans accents (É→e)."""
    return str(s or "").translate(_FOLD)


# Motifs par défaut, remplaçables/complétables via config.yaml (section parsers).
DEFAULT_PATTERNS = {
    "macrium": {
        "failure": [
            r"(?i)backup aborted", r"(?i)clone aborted",
            # Le digest « Backup Summary » de Site Manager donne des COMPTES,
            # pas des verdicts : « Failed backups: 0 » sous un tableau de
            # résultats. Le mot nu faisait passer pour une panne un digest qui
            # annonçait zéro échec (44 courriels du parc, 5 clients — vu le
            # 2026-07-28). C'est le compte qui décide ; le mot nu continue de
            # couvrir « Backup Failed », « Failed :( », « Failure ».
            # « Most recent failed operation: Image Backup 2025-10-05 » est
            # un champ d'HISTORIQUE du digest Site Manager : la dernière
            # panne connue, parfois vieille de dix mois. 58 courriels du parc
            # étaient comptés en panne pour ça (2026-07-28).
            r"(?i)(?<!most recent )\bfailed\b"
            r"(?![^\S\r\n]*backups?[^\S\r\n]*[:=])",
            r"(?i)failed[^\S\r\n]*backups?[^\S\r\n]*[:=][^\S\r\n]*[1-9]",
            r"(?i)échou", r"(?i)errors?\s*[:=]\s*[1-9]",
            r"(?i)completed with errors", r"(?i)cancell?ed", r"(?i)annulé",
            # Modèle de notification intégré de Macrium Reflect (« <machine>
            # Macrium Reflect - Backup Failure » / « Failure Notification »,
            # corps « Failed :( »). Motifs ancrés : un « Failure count: 0 »
            # dans un rapport de succès ne doit pas passer pour une erreur.
            r"(?i)backup\s+failure", r"(?i)clone\s+failure",
            # Sujet Site Manager « Notification - Backup Fail on computer X ».
            # Le libellé v7 s'arrête à « Fail » (sans « ed ») et ne matchait
            # RIEN, alors que la forme v8 « Backup Failed » passait déjà :
            # une console v7 restait muette. Le préfixe « Notification - »
            # ancre le motif en tête de sujet, le regard avant borne la fin
            # pour qu'un « Backup Failure Notification » ne compte pas deux
            # fois.
            r"(?i)notification[^\S\r\n]*-[^\S\r\n]*backup[^\S\r\n]*"
            r"fail(?:ed|ure)?(?=[^\S\r\n]*(?:[\r\n]|on[^\S\r\n]+computer\b|$))",
            r"(?i)failure\s+notification", r"(?i)failed\s*:\(",
            # Sauvegarde infonuagique (« "Backups S3" Errors occurred ») —
            # garde : « no errors occurred » resterait un succès.
            r"(?i)(?<!no )\berrors occurred\b",
        ],
        "warning": [
            r"(?i)completed with warnings", r"(?i)warnings?\s*[:=]\s*[1-9]",
            # Même précaution que côté Retrospect : le mot nu
            # « avertissement » matchait « 0 avertissements ».
            r"(?i)avec avertissements?",
            r"(?i)(?<![\d.\-/])[1-9]\d*[^\S\r\n]*avertissements?\b",
            r"(?i)backup\s+warning", r"(?i)warning\s+notification",
            # Sujet « … - Backup with Warnings », corps « Warning :| » —
            # l'émoticône tiède du modèle Macrium, comme « Failed :( ».
            r"(?i)backup with warnings", r"(?i)warning\s*:\|",
            # Macrium Site Manager : dépôt presque plein ou injoignable.
            r"(?i)disk space low",
            r"(?i)\buncontactable\b",
            # Digest « Backup Summary » : des machines qui n'ont PAS été
            # sauvegardées, et des agents déconnectés. Macrium les range
            # lui-même sous « Computer Warnings » — ce n'est pas un échec de
            # travail, mais ça ne doit surtout pas passer pour un succès.
            # (« Disconected », avec un seul n, est la faute du produit.)
            r"(?i)computers?[^\S\r\n]*not[^\S\r\n]*backed[^\S\r\n]*up"
            r"[^\S\r\n]*[:=][^\S\r\n]*[1-9]",
            r"(?i)disconn?ected[^\S\r\n]*[:=][^\S\r\n]*[1-9]",
            # Macrium Image Guardian : opération bloquée sur un fichier de
            # sauvegarde — la protection a fonctionné, mais un processus
            # non autorisé y a touché : à regarder. Texte documenté :
            # « Blocked unauthorised process (x.exe) accessing file (…) ».
            r"(?i)blocked file operation",
            r"(?i)blocked unauthori[sz]ed process",
        ],
        "success": [
            r"(?i)completed successfully", r"(?i)backup completed",
            r"(?i)réussi", r"(?i)succès",
            # Digest tout au vert : « Failed backups: 0 » et rien d'autre à
            # signaler. Sans ce motif, un digest sans échec ni avertissement
            # ne serait plus une erreur mais un INCONNU — on ne remplace pas
            # une fausse alerte par un trou.
            r"(?i)failed[^\S\r\n]*backups?[^\S\r\n]*[:=][^\S\r\n]*0\b",
            r"(?i)computers?[^\S\r\n]*backed[^\S\r\n]*up[^\S\r\n]*[:=]"
            r"[^\S\r\n]*[1-9]",
            # Idem, pendant succès : sujet « ... Backup Success » / « Success
            # Notification », corps « Success :) ».
            r"(?i)backup\s+success", r"(?i)success\s+notification",
            r"(?i)success\s*:\)",
        ],
        "extract": {
            # « (?:\s+name)? » : sur « Computer Name: SRV-X », capturer
            # SRV-X et non « Name ».
            "machine": [r"(?i)computer(?:\s+name)?\s*:?\s+([A-Za-z0-9._-]+)",
                        r"(?i)ordinateur\s*:?\s+([A-Za-z0-9._-]+)",
                        # Sujet type « SRV1(Backups) Macrium Reflect - ... » :
                        # le nom de machine précède « Macrium Reflect ».
                        r"^([A-Za-z0-9._+-]+)\s*(?:\([^)]*\))?\s*Macrium Reflect",
                        # « Macrium Image Guardian - Event - <machine> ».
                        r"(?i)image guardian - event - ([A-Za-z0-9._-]+)"],
            # « Backup definition: X » dans le corps quand il y est ; sinon la
            # parenthèse du sujet, « EX-Office-19(Exchange) Macrium Reflect -
            # … », qui porte le nom de la définition de sauvegarde. Sans
            # elle, la tâche restait vide sur 884 courriels du parc — donc
            # aucun regroupement possible par sauvegarde.
            # « Backup definition: 'X' », « Definition: Workstations ». Le
            # séparateur doit être sur la MÊME ligne : « [\s:'"]+ » franchissait
            # le saut de ligne et, dans le tableau du digest Site Manager,
            # capturait l'en-tête de la colonne suivante. D'où des tâches
            # fantômes dans le tableau — « Gilca — Description »,
            # « Jancor — Warnings » — qui n'existent chez personne.
            "job": [r"(?i)\bdefinition[^\S\r\n]*[:=][^\S\r\n]*['\"]?"
                    r"([^'\"\r\n]+)",
                    r"(?i)\bdefinition[^\S\r\n]*['\"]([^'\"\r\n]+)['\"]",
                    r"^[A-Za-z0-9._+-]+\s*\(([^)\r\n]+)\)\s*Macrium Reflect"],
        },
    },
    "retrospect": {
        "failure": [
            r"(?i)\bfailed\b", r"(?i)[ée]chec", r"(?i)\berror -?\d+",
            r"(?i)\berreur -?\d+", r"(?im)^!",
            # Nomenclature « ProActive - Remote - <Compagnie> - N erreurs » :
            # le compte d'erreurs du sujet donne le statut.
            # « [^\S\r\n]* » et non « \s* » : le texte classé est
            # « sujet \n corps », et « \s* » franchissait ce saut de ligne —
            # le digest « Retrospect : état pour 2026-07-26 » suivi d'un
            # corps commençant par « Erreurs : 0 » se lisait « 26 Erreurs »
            # et devenait une erreur alors que tout allait bien.
            # Le regard arrière refuse un chiffre ou un séparateur de date
            # collé devant : « 10 erreurs » ne peut pas se lire « 0 erreurs »
            # (et réciproquement), « le 26 erreurs » non plus.
            r"(?i)(?<![\d.\-/])[1-9]\d*[^\S\r\n]*(?:erreurs?|errors?)\b",
            # Récapitulatif de la console (FR/EN) : « * Erreurs: 3 * » ;
            # sujets « … - Notification d'erreur - Retrospect » et son
            # équivalent anglais documenté « … - Error Notification - … ».
            r"(?i)(?:erreurs?|errors?)\s*[:=]\s*[1-9]",
            r"(?i)notification[^\S\r\n]+d['’]erreur",
            r"(?i)error notification",
        ],
        "warning": [r"(?i)with warnings", r"(?i)avec avertissements?",
                    # « * Avertissements: 4 * » (récapitulatif FR/EN) —
                    # évalué APRÈS les erreurs : « Erreurs: 3, Avert.: 4 »
                    # reste une erreur.
                    r"(?i)avertissements?\s*[:=]\s*[1-9]",
                    r"(?i)warnings?\s*[:=]\s*[1-9]",
                    # Compte du sujet « … - 0 erreurs, 3 avertissements - … ».
                    # Le mot NU « avertissement » tenait cette place : il
                    # matchait « 0 avertissementS », et comme les
                    # avertissements passent avant les succès, tout sujet
                    # « 0 erreurs, 0 avertissements » (rien à signaler)
                    # ressortait en avertissement — 84 courriels dans le
                    # parc réel, et aucun succès Retrospect.
                    r"(?i)(?<![\d.\-/])[1-9]\d*[^\S\r\n]*"
                    r"(?:avertissements?|warnings?)\b",
                    # Digest quotidien « Retrospect : état pour <date> » :
                    # une sauvegarde interrompue par l'opérateur mérite un
                    # coup d'œil, pas un vert silencieux. Sujet anglais
                    # documenté : « Execution stopped by operator - … ».
                    r"(?i)interrompues? par l['’]op[ée]rateur\s*:\s*[1-9]",
                    r"(?i)stopped by operator",
                    # Console ProActive : « Script X (MACHINE) : Attente de
                    # support » — le script attend son média, la sauvegarde
                    # ne repartira pas seule (vu en réel chez Gilca,
                    # 2 courriels « inconnus »). Forme anglaise non observée
                    # ici — ne pas l'inventer.
                    r"(?i)attente de support"],
        "success": [
            r"(?i)completed successfully", r"(?i)terminé(e)? avec succès",
            r"(?i)normal execution", r"(?i)exécution normale",
            # Pendant du compte d'erreurs, mêmes gardes (pas de saut de
            # ligne traversé, pas de chiffre collé devant).
            r"(?i)(?<![\d.\-/])0[^\S\r\n]*(?:erreurs?|errors?)\b",
            # Notification d'entretien de la Management Console (vue en vrai
            # chez Ruscio Studio, 5 courriels) : « Optimisation de 579.5 Mo
            # du jeu de sauvegarde <jeu>. » C'est le grooming — Retrospect
            # libère de la place, aucune sauvegarde n'a eu lieu. Informatif,
            # donc succès comme les alertes « Information » de Backup Exec,
            # et surtout plus « inconnu ». Évalué en dernier : un courriel
            # qui contiendrait aussi une erreur reste une erreur.
            # Forme anglaise non observée ici — ne pas l'inventer.
            r"(?i)optimisation de .{1,40}? du jeu de sauvegarde",
            # Récapitulatif de la console FR/EN (« * Erreurs : 0 * ») —
            # évalué après failure/warning : « Erreurs : 3 » reste une
            # erreur, « Warnings: 2 » un avertissement.
            r"(?i)(?:erreurs?|errors?)\s*[:=]\s*0\b",
        ],
        "extract": {
            # « \bfrom\s+ » ancré ; l'ancien « de (…) » capturait n'importe
            # quel mot après « de » dans un corps français (« de sauvegarde »,
            # « de fichiers »…) et polluait l'association client.
            # « Client : SBSSERVER (…) » dans les notifications de la
            # console : c'est la machine sauvegardée.
            "machine": [r"(?i)\bfrom\s+([A-Za-z0-9._-]+)",
                        r"(?i)ordinateur\s+([A-Za-z0-9._-]+)",
                        r"(?i)\bclient\s*:\s*([A-Za-z0-9._-]+)"],
            # « Script "X" » (journal) et « Script : X Client : … » (console,
            # sans guillemets) : borné par « Client », « Date » ou la fin de
            # ligne, sinon la capture avalerait tout le corps.
            "job": [r"[Ss]cript\s+[«\"']([^»\"']+)",
                    r"(?i)script\s*:\s*(.+?)\s*(?:\bclient\s*:|\bdate\s*:|$)"],
            # Nomenclature « ProActive - Remote - <Compagnie> - N erreurs
            # [, M avertissements][ - Retrospect] » ou « … - Notification
            # d'erreur - Retrospect » : le CLIENT est le nom de compagnie du
            # sujet. Groupe paresseux + fin ancrée : un nom à trait d'union
            # (« Ste-Foy Dentaire ») reste entier. Préfixes RE:/TR: tolérés.
            "client": [r"(?i)^\s*(?:(?:re|tr|fwd?)\s*:\s*)*proactive\s*-\s*"
                       r"remote\s*-\s*(.+?)\s*-\s*"
                       r"(?:\d+\s*(?:erreurs?|errors?)"
                       r"(?:\s*,\s*\d+\s*(?:avertissements?|warnings?))?"
                       # « - Notification - Retrospect » (console) existe
                       # aussi sans « d'erreur » ; forme anglaise
                       # documentée « - Error Notification - ».
                       r"|(?:error\s+)?notification(?:\s+d['’]erreur)?)"
                       r"(?:\s*-\s*retrospect)?\s*$",
                       r"(?i)^\s*(?:(?:re|tr|fwd?)\s*:\s*)*proactive\s*-\s*"
                       r"remote\s*-\s*(.+?)\s*-\s*\d+\s*"
                       r"(?:erreurs?|errors?)\s*$"],
        },
    },
    # Systèmes fréquemment mélangés aux courriels Macrium/Retrospect dans une
    # boîte partagée (mode client_folders) : reconnus pour un statut ET un
    # libellé de produit exacts, plutôt qu'un classement générique « macrium ».
    "sqlagent": {
        # Modèle standard des notifications d'opérateur SQL Server Agent :
        # « [The job succeeded.] SQL Server Job System: '<job>' completed/
        # failed on <serveur>. »
        # Le SUJET ne porte pas le statut (toujours « completed on ») : la
        # vérité est dans le corps — « STATUS: Succeeded/Failed » et
        # « MESSAGES: The job succeeded/failed » (gabarit Microsoft).
        # LES DEUX MOTIFS CROCHETÉS ÉTAIENT MORTS. Le gabarit Microsoft écrit
        # « [The job failed.] » — avec un POINT avant le crochet fermant —
        # et « \[the job failed\] » ne matchait donc RIEN (reproduit le
        # 2026-08-06 sur le sujet réel du parc). Tout le produit reposait en
        # pratique sur « \bjob failed\b », le mot nu, qui a produit deux faux
        # échecs vérifiés : un travail NOMMÉ « Alerte si job failed » dans un
        # courriel de succès, et un digest portant « Job Failed: 0 » — la
        # régression « Failed backups: 0 » une seconde fois, sur les
        # 248 courriels du plus gros émetteur silencieux du parc.
        #
        # Les formes crochetées sont ancrées sur « SQL Server Job System: »
        # qui les suit TOUJOURS : le nom du travail vit après ce libellé,
        # entre apostrophes, donc un nom piégé ne peut plus déclencher.
        # « MESSAGES: The job failed. » couvre le corps APLATI (SQL 2000 sans
        # crochets, ou HTML dont les cellules ne rendent pas de saut de
        # ligne) là où « ^status: » échoue. Attention en le modifiant :
        # classify() écrase les espaces horizontaux AVANT de chercher, donc
        # le double espace du gabarit Microsoft ne doit jamais être écrit ici.
        "failure": [r"(?i)\[the job failed\.\][^\S\r\n]*"
                    r"sql server job system[^\S\r\n]*:",
                    r"(?i)\bmessages[^\S\r\n]*:[^\S\r\n]*the job failed\.",
                    r"(?im)^status:\s+failed"],
        "warning": [r"(?i)\[the job succeeded with warning",
                    # « The job was stopped prior to completion by … » :
                    # arrêt manuel, pas un échec d'exécution.
                    r"(?i)stopped prior to completion"],
        # Gardes anti-négation : « was not succeeded » ne doit pas passer
        # pour un succès (voir la même précaution sur GENERIC_PATTERNS).
        "success": [r"(?i)\[the job succeeded\.\][^\S\r\n]*"
                    r"sql server job system[^\S\r\n]*:",
                    r"(?i)\bmessages[^\S\r\n]*:[^\S\r\n]*the job succeeded\.",
                    r"(?im)^status:\s+succeeded"],
        "extract": {
            # « \\{0,2} » et non « \\{1,2} » : le gabarit écrit aussi
            # « completed on EdSQLServer » SANS contre-oblique, et la machine
            # sortait alors VIDE (reproduit). Sur « \\SQLDEV1\SQL2000 » on
            # capture SQLDEV1 — la machine, pas l'instance : garder
            # l'instance découperait un même serveur en deux machines.
            # « failed on … » est retiré : l'agent écrit TOUJOURS
            # « completed on », quel que soit le verdict — c'était du poids
            # mort qui laissait croire le cas d'échec couvert.
            "machine": [r"(?i)completed on[^\S\r\n]+\\{0,2}([A-Za-z0-9_-]+)"],
            # Le nom du travail peut lui-même contenir des apostrophes :
            # « 'DB Backup Job for DB Maintenance Plan 'SystemDBBkup'' ».
            # « [^']+ » s'arrêtait à la première interne et tronquait le nom,
            # regroupant deux plans de maintenance distincts sous une seule
            # tâche. Un groupe paresseux borné par son terminateur littéral
            # rend le nom entier.
            "job": [r"(?i)\bjob run[^\S\r\n]*:[^\S\r\n]*'(.+?)'[^\S\r\n]+"
                    r"was run on",
                    r"(?i)sql server job system[^\S\r\n]*:[^\S\r\n]*"
                    r"'(.+?)'[^\S\r\n]+completed on"],
        },
    },
    "pbs": {
        # Proxmox Backup Server / vzdump : sauvegardes VM (vzdump), purge
        # (Garbage Collect), rétention (Pruning), réplication (Sync remote)
        # et sauvegardes d'hôte via proxmox-backup-client (courriels
        # « Timer service … pbs.sh » : journal du client, « Error: » en
        # début de ligne = échec, « End Time: » atteint = terminé).
        # Le mot nu « failed » a été RETIRÉ : le corps d'un vzdump est un
        # JOURNAL, et un vzdump parfaitement réussi y écrit « ERROR:
        # fs-freeze failed - command timed out » avant de conclure
        # « Backup job finished successfully ». Reproduit le 2026-08-06 : la
        # sauvegarde sortait en PANNE. Même famille que les régressions
        # Macrium « Failed backups: 0 » et S3 « Failed: 140/17 ».
        # À la place, les lignes de VERDICT littérales des gabarits Proxmox
        # (proxmox-backup, templates de notification), ancrées en début de
        # ligne : elles ne peuvent pas venir du corps d'un journal.
        "failure": [r"(?i)\bbackup failed\b", r"(?i)\btask error\b",
                    r"(?im)^(?:synchronization|verification|garbage "
                    r"collection|pruning|tape backup) failed[^\S\r\n]*:",
                    # Les sujets d'échec de PBS : « Sync remote 'X'
                    # datastore 'Y' failed », « Verify datastore 'Y'
                    # failed »… — une seule forme les couvre tous.
                    r"(?im)^[^\r\n]*\bdatastore '[^']*' failed[^\S\n]*$",
                    # Réplication PVE : « Replication job 100-0 failed ».
                    r"(?im)^[^\r\n]*\breplication job\b[^\r\n]*\bfailed\b",
                    r"(?im)^error\s*:"],
        "warning": [],
        # Le mot nu « successful » a été retiré lui aussi, et c'est le plus
        # important des deux : le courriel d'une réplication PVE MORTE porte
        # « Last successful sync: 2026-08-01 » — un faux succès n'attendait
        # que le retrait du « failed » ci-dessus pour s'ouvrir. Les formes
        # littérales des gabarits se terminent par un POINT, ce que ne fait
        # aucun champ « Last successful … ».
        "success": [r"(?im)^(?:synchronization|verification|garbage "
                    r"collection|pruning|tape backup) successful\.",
                    r"(?i)\bbackup job finished successfully\b",
                    r"(?im)^end time\s*:"],
        "extract": {
            "machine": [r"\(([A-Za-z0-9_.-]+)\)",
                        r"(?i)client name\s*:\s*([A-Za-z0-9_.-]+)"],
            "job": [r"[Dd]atastore\s+'([^']+)'"],
        },
    },
    "cobian": {
        # Cobian Reflector / Cobian Backup : courriel « automatic mail
        # message from Cobian Reflector » avec le journal dans le corps.
        # Statuts déduits du format de journal (Errors: N) — à confirmer.
        # Le mot nu « failed » a été RETIRÉ. Le corps est le JOURNAL complet,
        # avec le chemin de chaque fichier copié : un dossier nommé
        # « C:\Data\Failed transfers\ » suffisait à faire d'une sauvegarde
        # saine une panne — reproduit le 2026-08-06, ce motif était le SEUL
        # à se déclencher. « with errors » et « successfully done » partent
        # aussi : introuvables dans les fichiers de langue du produit comme
        # dans les journaux réels, ils ne protégeaient rien.
        # Les phrases de fin de journal, elles, sont des VERDICTS, et elles
        # vont par paire : adopter celle de succès sans celle d'échec ferait
        # virer au vert un journal multi-tâches dont le préfixe « ERR » a
        # sauté au transport.
        "failure": [r"(?i)(?:erreurs?|errors?)\s*[:=]\s*[1-9]",
                    r"(?i)the backup has ended\W{0,4}there are errors\b",
                    # En-tête du courriel par tâche de Reflector :
                    # « …, had 3 errors ». Compteur non nul exigé.
                    r"(?i),[^\S\r\n]*had[^\S\r\n]+[1-9]\d*[^\S\r\n]+errors?\b",
                    # Lignes de détail du journal : préfixe « ERR » en tête
                    # de ligne (avant l'horodatage).
                    r"(?im)^[^\S\r\n]*err[^\S\r\n]"],
        "warning": [r"(?i)warnings?\s*[:=]\s*[1-9]\d*"],
        "success": [r"(?i)errors?\s*[:=]\s*0\b",
                    r"(?i)the backup has ended[^\S\r\n]+without errors\b"],
        "extract": {
            # Sujet type « Backup Summum (SERVEUR-PC) » : tâche + machine.
            # La ligne de fin de journal « Backup done for the task "X" »
            # est plus fiable que le sujet (libre chez Cobian).
            #
            # Le sujet PAR DÉFAUT du produit est « Cobian Backup 11
            # [SERVEUR-PC] (2026-08-06) » : la parenthèse finale y porte la
            # DATE, pas la machine — l'outil créait donc une machine
            # fantôme par nuit (reproduit). Les crochets passent d'abord ;
            # la parenthèse ne sert plus de repli que si elle contient au
            # moins une lettre hors motif de date.
            "machine": [r"(?i)\[([A-Za-z0-9._-]+)\][^\S\r\n]*"
                        r"(?:\([^)\r\n]*\))?[^\S\r\n]*$",
                        r"\((?!\d{2,4}[-/]\d{1,2}[-/]\d{1,4}\s*\))"
                        r"([A-Za-z0-9._ -]*[A-Za-z][A-Za-z0-9._ -]*)\)"
                        r"[^\S\r\n]*$"],
            "job": [r"(?i)backup done for the task \"([^\"]+)\"",
                    # En-tête de section du journal, présent même quand la
                    # ligne de fin manque (journal tronqué au transport).
                    r"(?i)\*\*[^\S\r\n]*(?:backing up the task|backup for "
                    r"task)[^\S\r\n]*\"([^\"\r\n]+)\"",
                    r"(?i)^backup\s+(.+?)\s*\("],
        },
    },
    "backupexec": {
        # Veritas Backup Exec : « Backup Exec Alert: <catégorie> (Server:
        # "X") (Job: "Y") ». Sévérités officielles par catégorie (guide
        # 22.1 + BEMCLI) : « Job Cancellation » et les catégories « …
        # Error/Failure » = erreur ; « … Warning », « Media Insert/… » et
        # « Backup job contains no data » = avertissement ; « Missed » =
        # planifié mais jamais exécuté.
        "failure": [r"(?i)job completion status\s*:\s*"
                    r"(?:failed|error|missed|cancell?ed)",
                    # « Completed status: Failed/Canceled » — le champ de
                    # verdict du RAPPORT DE TÂCHE, distinct du « Job
                    # Completion Status » des alertes. Trou réel : ce
                    # libellé sortait « inconnu », donc invisible du triage.
                    r"(?i)\bcompleted status[^\S\r\n]*:[^\S\r\n]*"
                    r"(?:failed|cancell?ed|missed)",
                    r"(?i)\bjob failed\b",
                    r"(?i)the job was cancell?ed",
                    r"(?i)backup exec alert:\s*"
                    r"(?:job cancell?ation|[a-z ]*\b(?:error|failure)\b)"],
        "warning": [
            # PIÈGE documenté : le courriel « exceptions » commence par
            # « The job completed successfully.  However, the following
            # conditions were encountered: … » — évalué AVANT le motif
            # succès « completed successfully ».
            r"(?i)however, the following conditions were encountered",
            r"(?i)with exceptions",
            r"(?i)backup exec alert:\s*[a-z ]*\bwarning\b",
            r"(?i)backup exec alert:\s*(?:media (?:intervention|insert|"
            r"overwrite|remove)|library insert)",
            r"(?i)backup job contains no data",
        ],
        # Les catégories « … Information » (General Information, Database
        # Maintenance Information, observées en réel) sont la sévérité la
        # plus basse de Backup Exec : purement informatives, jamais un
        # échec — on suit sa taxonomie, comme le « 0 erreurs » Retrospect.
        # « Job Success » + corps « Completed Successfully. » : la forme
        # réelle observée (pas de « Job Completion Status » dans le corps).
        "success": [r"(?i)job completion status\s*:\s*success",
                    r"(?i)\bcompleted status[^\S\r\n]*:[^\S\r\n]*"
                    r"successful\b",
                    r"(?i)backup exec alert:\s*job success",
                    r"(?i)\bcompleted successfully\b",
                    r"(?i)backup exec alert:\s*[a-z ]*\binformation\b",
                    # « Service Start » (vu en réel chez Seanautic) : le
                    # service a été démarré par un administrateur —
                    # informatif, comme les catégories « … Information ».
                    # « Service Stop » n'est PAS couvert : un arrêt peut
                    # être une panne, il doit rester visible (inconnu).
                    r"(?i)backup exec alert:\s*service start\b"],
        "extract": {
            # Guillemet FACULTATIF : les alertes écrivent « Server: "X" »
            # mais le rapport de tâche écrit « Job server: SILVER », sans
            # guillemets — la machine sortait vide sur ce gabarit.
            "machine": [r"(?i)server[^\S\r\n]*:[^\S\r\n]*\"?"
                        r"\\{0,2}([A-Za-z0-9._-]+)"],
            "job": [r"(?i)job:\s*\"([^\"]+)"],
        },
    },
    "script": {
        # Scripts maison à convention « [Success]/[Failed]/[Warning] » en
        # préfixe de sujet (ex. rapports générés par un outil interne).
        # Formes réelles du parc (Veeam Agent derrière un script, client
        # Azimuth) : sujet « AZIMUTH - [Failed] ATELIER - ATELIER -
        # Mascouche - Thursday, 30 July 2026 21:54:28 », corps « Agent
        # Backup job: ATELIER - Mascouche Veeam Agent for Microsoft
        # Windows ». Machine et tâche restaient vides sur 30 courriels —
        # aucun regroupement possible (rapport diagnostic du 2026-08-05).
        "failure": [r"(?i)\[failed\]", r"(?i)\[error\]"],
        "warning": [r"(?i)\[warning\]"],
        "success": [r"(?i)\[success\]", r"(?i)\[ok\]"],
        "extract": {
            # Le premier mot après le verdict : « [Success] AZIMUTH-001 - »,
            # « [Failed] ATELIER - ».
            "machine": [r"(?i)\[(?:success|failed|warning|error|ok)\]"
                        r"[^\S\r\n]*([A-Za-z0-9._-]+)"],
            # La tâche vient du corps (« [Agent] Backup job: X »), bornée à
            # SA ligne — « Veeam Agent … » qui la suit n'en fait pas partie.
            # Repli : le sujet, entre la machine et la date en toutes
            # lettres (jours anglais observés ; ne pas inventer le français).
            "job": [r"(?im)\bbackup job:[^\S\r\n]*(.+?)"
                    r"[^\S\r\n]*(?:veeam agent\b.*)?$",
                    r"(?i)\[(?:success|failed|warning|error|ok)\]"
                    r"[^\S\r\n]*[A-Za-z0-9._-]+[^\S\r\n]*-[^\S\r\n]*(.+?)"
                    r"[^\S\r\n]*-[^\S\r\n]*"
                    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day\b"],
        },
    },
    "acronis": {
        # Acronis Drive Monitor : alertes de SANTÉ DISQUE relayées dans la
        # boîte de sauvegarde (« Alerte Acronis Drive Monitor : Messages
        # critiques du journal des événements Windows sur <machine> »).
        # Ce ne sont pas des résultats de sauvegarde : le verdict est le
        # champ « Risque » du corps, un par événement relaté. Observé en
        # réel (Propulsion+, 2 courriels « inconnus ») : « Risque : Faible »
        # sur des événements Volsnap purement informatifs → succès, comme
        # les alertes « Information » de Backup Exec. Les niveaux
        # supérieurs sont DÉDUITS de la gamme du produit, jamais vus ici —
        # produit listé dans les cas à confirmer du rapport diagnostic.
        # L'ordre des motifs fait le bon verdict pour un digest mélangé :
        # un seul « Risque : Élevé » parmi des « Faible » = avertissement.
        "failure": [],
        "warning": [r"(?i)risque\s*:\s*(?:moyen|[ée]lev[ée]|critique)"],
        "success": [r"(?i)risque\s*:\s*faible"],
        "extract": {
            "machine": [r"(?i)journal des [ée]v[ée]nements windows sur"
                        r"[^\S\r\n]+([A-Za-z0-9._-]+)"],
            "job": [],
        },
    },
    # Captures d'écran analysées (captures.py) : le sujet est SYNTHÉTISÉ par
    # l'outil, pas reçu d'un tiers — d'où des motifs stricts plutôt que
    # tolérants. Ces marqueurs et ceux de captures.MARQUEURS doivent rester
    # d'accord ; l'autotest le vérifie sur un aller-retour complet.
    "capture": {
        "failure": [r"\[Failed\]"],
        "warning": [r"\[Warning\]"],
        "success": [r"\[Success\]"],
        "extract": {
            "machine": [r"Machine:\s*([^|]+?)\s*(?:\||$)"],
            "job": [r"Tache:\s*(.+?)\s*$"],
        },
    },
}

_ORDER = [("failure", STATUS_ERROR), ("warning", STATUS_WARNING),
          ("success", STATUS_SUCCESS)]

# Filet de sécurité générique : appliqué en dernier recours (après les motifs
# Macrium/Retrospect) pour les courriels d'AUTRES systèmes qui atterrissent
# dans les mêmes dossiers clients (mode client_folders) — jobs SQL Server
# Agent, Proxmox Backup Server (vzdump), scripts maison à convention
# « [Success]/[Failed] ». Les gardes « (?<!not )/(?<!non ) » évitent de
# classer « not successful »/« non réussi » comme un succès.
GENERIC_PATTERNS = {
    "failure": [
        r"(?i)\bjob failed\b", r"(?i)\[failed\]", r"(?i)\btask error\b",
    ],
    "warning": [
        r"(?i)\[warning\]",
        # « Succeeded with warnings » — le statut mixte que presque tous les
        # produits emploient (Acronis le documente aussi pour une tâche
        # ANNULÉE, KB 35088). Sans lui, le succès générique juste en dessous
        # peignait la phrase en VERT : faux succès reproduit le 2026-08-06
        # sur un dossier épinglé « product: macrium », et même en mode auto
        # pour la forme au singulier. Un vert sur une sauvegarde annulée est
        # exactement ce que cet outil existe pour empêcher. Le singulier et
        # le pluriel sont couverts par le même motif.
        r"(?i)\bsucceeded\s+with\s+warnings?\b",
    ],
    "success": [
        r"(?i)(?<!not )(?<!non )(?<!sans )(?<!without )\bsuccessful\b",
        # Le regard avant est INDISSOCIABLE du motif d'avertissement
        # ci-dessus : sans lui, « succeeded with warnings » matcherait ici
        # aussi. L'ordre de _ORDER protège déjà (warning avant success),
        # mais un motif de succès qui matche une phrase d'avertissement est
        # un piège armé pour le jour où l'ordre changera.
        r"(?i)(?<!not )(?<!non )\bsucceeded\b(?!\s+with\s+warning)",
        r"(?i)\[success\]",
    ],
}


def _generic_compiled() -> dict:
    global _GENERIC_COMPILED
    if _GENERIC_COMPILED is None:
        _GENERIC_COMPILED = {k: _compile_all(v)
                             for k, v in GENERIC_PATTERNS.items()}
    return _GENERIC_COMPILED


_GENERIC_COMPILED = None


def _compile_all(patterns: list) -> list:
    """Compile une liste de motifs ; un motif invalide est simplement ignoré
    (il ne matcherait jamais — même comportement qu'avant, sans le coût d'une
    exception à chaque courriel)."""
    out = []
    for pat in patterns or []:
        try:
            out.append(re.compile(pat))
        except re.error:
            continue
    return out


def _patterns_for(cfg: dict, product: str) -> dict:
    """Motifs COMPILÉS d'un produit (défauts fusionnés avec config.yaml).
    Compilés une seule fois par exécution — sur ~1300 courriels × ~30 motifs,
    recompiler à chaque courriel dominait le temps d'analyse. Le cache est
    invalidé si la section parsers change d'objet (autre config)."""
    src = cfg.get("parsers")
    cache = cfg.get("_compiled_patterns")
    if cache is None or cache.get("_src") is not src:
        cache = {"_src": src}
        cfg["_compiled_patterns"] = cache
    if product in cache:
        return cache[product]
    user = (src or {}).get(product) or {}
    base = DEFAULT_PATTERNS.get(product, {})
    merged = {}
    for key in ("failure", "warning", "success"):
        merged[key] = _compile_all(user.get(key) if user.get(key)
                                   else base.get(key, []))
    extract = dict(base.get("extract", {}))
    extract.update(user.get("extract") or {})
    merged["extract"] = {k: _compile_all(v) for k, v in extract.items()}
    cache[product] = merged
    return merged


def _first_match(patterns: list, text: str) -> str | None:
    for rx in patterns or []:
        if rx.search(text):
            return rx.pattern
    return None


def _extract(patterns: list, *texts: str) -> str:
    for text in texts:
        for rx in patterns or []:
            m = rx.search(text)
            if m:
                return (m.group(1) if m.groups() else m.group(0)).strip()
    return ""


def _known_products(cfg: dict) -> list[str]:
    names = list(DEFAULT_PATTERNS.keys())
    for p in (cfg.get("parsers") or {}):
        if p not in names:
            names.append(p)
    return names


def _detect_product(cfg: dict, text: str) -> str:
    """Devine le produit d'un courriel issu d'un dossier « auto » (produits
    mixtes). D'abord une signature de produit connu dans le texte ; sinon le
    jeu de motifs qui parvient à classer le courriel ; sinon « macrium » par
    défaut."""
    low = text.lower()
    if "retrospect" in low:
        return "retrospect"
    # « ProActive » (sauvegarde proactive Retrospect) — nomenclature
    # « ProActive - Remote - <Compagnie> - N erreurs » sans le mot
    # « Retrospect » dans le texte.
    if re.search(r"\bproactive\b", low):
        return "retrospect"
    # AVANT le test macrium/reflect : « Cobian Reflector » contient
    # « reflect » et passait à tort pour du Macrium.
    if "cobian" in low:
        return "cobian"
    # AVANT le repli macrium : les alertes de santé disque d'Acronis Drive
    # Monitor n'ont aucun mot Macrium et tombaient sur le produit par
    # défaut. « drive monitor » précisément — un courriel Acronis Cyber
    # Backup, jamais observé ici, ne doit pas hériter de ces motifs.
    if "acronis drive monitor" in low:
        return "acronis"
    if "backup exec" in low:
        return "backupexec"
    if "macrium" in low or "reflect" in low:
        return "macrium"
    if "sql server job system" in low:
        return "sqlagent"
    if ("vzdump" in low or "garbage collect datastore" in low
            or "pruning datastore" in low
            or ("sync remote" in low and "datastore" in low)
            # proxmox-backup-client (sauvegarde d'hôte via timer systemd)
            or "starting backup protocol" in low
            or "proxmox-backup-client" in low):
        return "pbs"
    if re.search(r"\[(success|failed|warning|error|ok)\]", low):
        return "script"
    for product in _known_products(cfg):
        pats = _patterns_for(cfg, product)
        for key, _ in _ORDER:
            if _first_match(pats.get(key), text):
                return product
    return "macrium"


def _problem_context(text: str, m) -> str:
    """Texte autour du motif d'erreur/avertissement déclencheur — c'est LA
    ligne utile (« erreur -519 (échec de la communication réseau) »), à
    montrer d'emblée plutôt que noyée dans l'extrait du corps."""
    start = max(0, m.start() - 40)
    frag = re.sub(r"\s+", " ", text[start:m.end() + 150]).strip()
    if start > 0 and " " in frag:
        # Ne pas commencer en plein mot : couper au premier espace.
        frag = "… " + frag.split(" ", 1)[1]
    return frag[:200]


# ─── Signature de problème ────────────────────────────────────────────────
# Le champ « problem » est un extrait de texte : bon à lire, inutilisable
# pour regrouper. Deux courriels du MÊME incident diffèrent par leur GUID,
# leur nom de fichier, leur taille ou leur horodatage — Image Guardian en est
# l'exemple type. On en tire donc une signature stable, en trois niveaux du
# plus fiable au plus général : un code d'erreur du produit, sinon un cas
# connu décrit par un motif et un libellé lisible, sinon le texte normalisé.

# Codes d'erreur. « propre » = identifiant émis par le produit lui-même, donc
# réellement diagnostique : il fait signature à lui seul. Les HRESULT Windows
# ne le sont PAS — 0x80070005 signifie « accès refusé » et coiffe des pannes
# sans rapport — ils ne priment donc jamais sur un cas reconnu, et exigent un
# mot-clé d'erreur à proximité pour ne pas confondre un jeton hexadécimal
# quelconque du courriel avec une cause.
PROBLEM_CODES = [
    # Retrospect : « erreur -519 (échec de la communication réseau) ».
    (r"(?i)\b(?:erreurs?|errors?)\s*(-\d{3,5})\b", True),
    # Backup Exec : « V-79-57344-33928 ».
    (r"\b(V-\d{1,3}-\d{1,6}(?:-\d{1,6})?)\b", True),
    # Windows / VSS / Macrium : « … error 0x80070005 ».
    (r"(?i)\b(?:error|erreur|code|hresult|status|statut)\b[^\r\n]{0,40}?"
     r"\b(0[xX][0-9A-Fa-f]{6,8})\b", False),
]

# Cas connus. Le troisième champ est le numéro du groupe qui capture l'OBJET
# du problème (dépôt, volume, machine) quand le gabarit le donne : sans lui,
# deux dépôts distincts injoignables partageraient une seule signature et
# leur antériorité serait mêlée. Ordre = du plus spécifique au plus général,
# mais c'est la PROXIMITÉ du motif déclencheur qui départage (voir plus bas),
# pas la position dans cette liste.
PROBLEM_KNOWN = [
    (r"(?i)blocked (?:file operation|unauthori[sz]ed process)",
     "Image Guardian : opération bloquée sur un fichier de sauvegarde", 0),
    (r"(?i)repository\s+([A-Za-z0-9._-]+)\s+has become uncontactable",
     "Dépôt injoignable", 1),
    (r"(?i)\buncontactable\b|dépôt injoignable", "Dépôt injoignable", 0),
    # Macrium, phrase officielle du moteur (article KB éponyme) : la
    # destination n'a pas pu être écrite — USB endormi, partage disparu,
    # identifiants réseau absents. Le regroupement, lui, fonctionnait déjà
    # (vérifié : deux postes vers le même dépôt partageaient la clé
    # « txt:backup aborted none of the specified backup locations ») ; ce qui
    # manquait est le LIBELLÉ et sa marche à suivre — une clé « txt: » n'a
    # aucune action associée, donc le tableau du matin affichait le problème
    # sans dire quoi faire. L'action de « Dépôt injoignable » couvre déjà les
    # trois causes du KB, mot pour mot : rien à écrire de plus.
    (r"(?i)none of the specified backup locations could be written to",
     "Dépôt injoignable", 0),
    (r"(?i)disk space low|espace disque faible", "Espace disque faible", 0),
    # Digest Site Manager : des postes n'ont pas été sauvegardés du tout, et
    # leurs agents sont déconnectés. Sans ce cas, la signature était le texte
    # normalisé du digest — illisible dans le tableau (45 courriels du parc,
    # 6 tâches, vu le 2026-07-28).
    (r"(?i)computers?[^\S\r\n]*not[^\S\r\n]*backed[^\S\r\n]*up"
     r"[^\S\r\n]*[:=][^\S\r\n]*[1-9]"
     r"|disconn?ected[^\S\r\n]*[:=][^\S\r\n]*[1-9]",
     "Machines non sauvegardées", 0),
    (r"(?i)(?:not enough|insufficient)\s+(?:disk\s+)?space[^\r\n]{0,30}?"
     r"\bvolume\s+([A-Za-z]:?)", "Espace disque insuffisant", 1),
    # « The disk is out of free space » / « … out of space » : la paire
    # d'alertes Storage Warning/Error de Backup Exec (vue en réel chez
    # Seanautic, 4 courriels). Sans ce membre, la signature retombait sur le
    # texte normalisé — nom du serveur compris — et le même disque plein
    # sortait sous deux signatures de charabia.
    (r"(?i)(?:not enough|insufficient)\s+(?:disk\s+)?space|espace insuffisant"
     r"|disque (?:plein|satur[ée])|(?:disk|volume|drive) (?:is )?full"
     r"|(?:disk|volume|drive) is out of (?:free )?space",
     "Espace disque insuffisant", 0),
    (r"(?i)communication failure|échec de la communication"
     r"|network(?:ing)? (?:error|failure)",
     "Échec de communication réseau", 0),
    # CODES OFFICIELS RETROSPECT. Le produit numérote ses pannes ; l'outil
    # les reconnaissait déjà par leurs PHRASES, mais un courriel qui ne porte
    # que le code sortait « Code -530 » — sans libellé lisible et sans marche
    # à suivre. Ces trois entrées n'influencent JAMAIS le statut (PROBLEM_KNOWN
    # n'étiquette qu'un courriel déjà classé) et réutilisent des libellés
    # DÉJÀ dans ACTIONS_CONNUES : rien de nouveau à écrire, la clé de
    # regroupement reste « code:-NNNN », l'historique ne bouge pas.
    # Le tiret est EXIGÉ (demi-cadratin accepté) : sans lui, « Erreurs: 1115 »
    # — un compteur de fichiers — se lirait comme un code. Le \b final
    # empêche -11150 de passer pour -1115.
    (r"(?i)\b(?:erreurs?|errors?)[^\S\r\n]*[-–][^\S\r\n]*1115\b",
     "Espace disque insuffisant", 0),
    (r"(?i)\b(?:erreurs?|errors?)[^\S\r\n]*[-–][^\S\r\n]*1204\b",
     "Intervention requise sur le média", 0),
    # -503 client éteint, -530 client introuvable, -541 client non installé :
    # trois codes, un seul phénomène — la machine ne répond plus à son
    # serveur de sauvegarde. La panne la plus banale d'un parc de portables.
    (r"(?i)\b(?:erreurs?|errors?)[^\S\r\n]*[-–][^\S\r\n]*(?:503|530|541)\b",
     "Connexion perdue avec l'agent", 0),
    # « Backup aborted! - Write operation failed - Le chemin réseau n'a pas
    # été trouvé » (Macrium vers un partage disparu, vu en réel chez Hogue).
    # Distinct de « Échec de communication réseau » : ici la destination
    # n'existe plus (nom, DNS, partage), rien à retenter tel quel.
    (r"(?i)chemin r[ée]seau n['’]a pas [ée]t[ée] trouv[ée]"
     r"|network path (?:was )?not found", "Chemin réseau introuvable", 0),
    (r"(?i)sync(?:hronization)? failed", "Réplication en échec", 0),
    # « Attente de support » : script Retrospect arrêté en attendant son
    # média (console ProActive, vu en réel chez Gilca). Même cas que les
    # demandes de média Backup Exec — la sauvegarde ne repartira pas seule.
    (r"(?i)media (?:intervention|required|insert|overwrite|remove)"
     r"|library insert|attente de support",
     "Intervention requise sur le média", 0),
    # « Access to network share \\NAS-01\Images was denied » : le verbe est
    # loin du sujet, un « access denied » collé ne suffit pas.
    (r"(?i)\baccess\b[^\r\n]{0,40}?\bdenied\b|\bpermission denied\b"
     r"|acc[èe]s refus[ée]", "Accès refusé", 0),
    (r"(?i)\bvss\w*|shadow copy|cliché instantané|snapshotset",
     "Cliché instantané VSS", 0),
    # « the connected party did not properly respond after a period of
    # time » : le libellé Windows du délai de connexion dépassé (WSAETIMEDOUT
    # 10060), sans le mot « timeout » — 7 courriels S3 du parc regroupés
    # sous un texte normalisé tronqué en plein milieu de phrase.
    (r"(?i)tim(?:e|ed)\s?out|délai d'attente"
     r"|connected party did not properly respond", "Délai dépassé", 0),
    (r"(?i)cannot (?:open|read|write)|unable to (?:open|read|write)"
     r"|impossible (?:d'ouvrir|de lire|d'écrire)",
     "Fichier illisible ou inaccessible", 0),
    (r"(?i)was cancell?ed|stopped by operator"
     r"|interrompues? par l['’]op[ée]rateur", "Interrompu par l'opérateur", 0),
    (r"(?i)contains no data|aucune donnée", "Sauvegarde sans données", 0),
    # Site Manager : l'agent Macrium de la machine ne répond plus. DEUX
    # gabarits pour ce seul phénomène — « Unable to monitor progress …
    # Lost connection with the agent » et « Unknown Operation failed …
    # Error - Agent disconnected » — qui sortaient sous deux signatures de
    # charabia pour le même portable injoignable (rapport du 2026-08-05,
    # LAPTOP-03 chez Jasmine Lindsay). Une seule signature, un seul suivi.
    (r"(?i)lost connection with the agent|agent disconnected"
     r"|agent d[ée]connect[ée]|unable to monitor progress of",
     "Connexion perdue avec l'agent", 0),
    # Veeam Agent relayé par script (« AZIMUTH - [Failed] … ») : le corps ne
    # donne que « Error » et des compteurs — la cause vit dans la console
    # Veeam. La signature textuelle recopiait le nom de la tâche en soupe de
    # mots (« failed atelier atelier mascouche agent backup job atelier »).
    (r"(?i)veeam agent for (?:microsoft )?windows\s+error\b"
     r"(?![^\S\r\n]*\d)",
     "Échec d'une tâche Veeam Agent", 0),
    # Rapport de vérification S3/BackupAssist en français : que des
    # COMPTEURS (« 2 Avertissement importants », « 1 Erreur très
    # critique ») — aucun texte de cause. Un seul cas pour les deux
    # phrases : la première rencontrée ferait sinon deux signatures pour le
    # même rapport (rapport du 2026-08-05, Vertika).
    # Garde de compteur non nul, même famille que « Failed backups: 0 » :
    # un « 0 Avertissement important » placé EN TÊTE volait le libellé à la
    # vraie cause située plus bas, puisque problem_signature départage à la
    # POSITION du motif. Le regard arrière n'exige pas de compteur — la
    # phrase nue reste reconnue —, il refuse seulement le compteur à zéro.
    (r"(?i)(?<!\b0[^\S\r\n])"
     r"(?:avertissements?\s+importants?|erreurs?\s+tr[èe]s\s+critiques?)",
     "Erreurs au rapport de vérification", 0),
    # Modèle de notification intégré de Macrium Reflect : le corps ne
    # contient QUE le verdict et son émoticône (« Failure :( »,
    # « Échoué :( », « Warning :| ») — aucune cause. La signature textuelle
    # en tirait TROIS charabias pour le même phénomène (« failed failure »,
    # « failure notification failure », « echoue ») — 79 courriels du parc
    # (rapport diagnostic du 2026-08-05). Une vraie cause présente dans le
    # texte prime toujours : le départage se fait à la POSITION du motif, et
    # l'émoticône arrive en dernier.
    (r"(?i)(?:failure|failed|échou[ée]e?)\s*:\(",
     "Échec de sauvegarde sans détail", 0),
    (r"(?i)(?:warning|avertissement)\s*:\|",
     "Avertissement sans détail", 0),
]

# Courriel Retrospect « Notification d'erreur » dont NI le contexte NI le
# courriel entier ne livrent de cause (ni code, ni cas connu) : le gabarit
# HTML de Retrospect fait alors dériver la signature textuelle sur le pied de
# page (l'URL retrospect.com) — 12 courriels du parc regroupés sous du
# boilerplate, sans libellé ni action (vu le 2026-07-29). Signature synthétique
# stable et lisible à la place.
LIB_NOTIF_SANS_DETAIL = "Notification d'erreur sans détail"

# Pendant SQL Server Agent : travail en échec dont le courriel ne porte aucune
# cause exploitable. Voir la branche correspondante dans classify().
LIB_SQL_SANS_DETAIL = "Échec d'un travail SQL Server Agent"

# Quoi faire, pour chaque cas reconnu. Écrit d'avance, jamais deviné : le
# tableau du matin doit dire quoi ouvrir en premier, pas seulement ce qui
# cloche. Clé = libellé de PROBLEM_KNOWN (l'autotest vérifie qu'aucun libellé
# ne reste sans action, sinon la colonne se viderait en silence).
ACTIONS_CONNUES = {
    LIB_NOTIF_SANS_DETAIL:
        "Ouvrir le journal du script dans Retrospect : le courriel ne donne "
        "pas la cause.",
    LIB_SQL_SANS_DETAIL:
        "Ouvrir l'historique du travail dans SQL Server Agent (Management "
        "Studio → SQL Server Agent → Travaux → Historique) : le courriel ne "
        "donne pas la cause.",
    "Image Guardian : opération bloquée sur un fichier de sauvegarde":
        "Identifier le processus bloqué (antivirus, indexation) et l'exclure "
        "du dossier de sauvegarde.",
    "Dépôt injoignable":
        "Vérifier que le NAS/partage répond et que les identifiants du dépôt "
        "sont encore valides.",
    "Machines non sauvegardées":
        "Ouvrir Site Manager : ces postes n'ont aucune copie du jour "
        "(agent arrêté, machine éteinte ou hors réseau).",
    "Espace disque faible":
        "Libérer de l'espace sur le dépôt ou réduire la rétention avant que "
        "les sauvegardes échouent.",
    "Espace disque insuffisant":
        "Libérer de l'espace sur le volume de destination, puis relancer la "
        "sauvegarde à la main.",
    "Échec de communication réseau":
        "Vérifier le lien vers la machine source (agent démarré, pare-feu, "
        "VPN) puis relancer.",
    "Chemin réseau introuvable":
        "Vérifier que le partage de destination existe encore et répond "
        "(nom, DNS, droits), puis relancer.",
    "Échec de sauvegarde sans détail":
        "Ouvrir le journal de la tâche sur la machine : la notification ne "
        "donne pas la cause.",
    "Avertissement sans détail":
        "Ouvrir le journal de la tâche sur la machine : la notification ne "
        "précise pas l'avertissement.",
    "Connexion perdue avec l'agent":
        "Vérifier que la machine est allumée et sur le réseau, et que son "
        "agent de sauvegarde tourne, puis relancer la tâche.",
    "Échec d'une tâche Veeam Agent":
        "Ouvrir la console Veeam Agent sur la machine : le courriel ne donne "
        "que le verdict, le journal de la tâche a la cause.",
    "Erreurs au rapport de vérification":
        "Ouvrir le rapport détaillé de la tâche : le courriel ne donne que "
        "des compteurs — la section en erreur (sélections, support) y est "
        "nommée.",
    "Réplication en échec":
        "Vérifier le dépôt distant et le lien de synchronisation ; la copie "
        "hors site n'est plus à jour.",
    "Intervention requise sur le média":
        "Insérer, remplacer ou libérer le média demandé dans la librairie.",
    "Accès refusé":
        "Vérifier les droits du compte de service sur la source et sur le "
        "dépôt (mot de passe expiré, partage modifié).",
    "Cliché instantané VSS":
        "Redémarrer les services VSS de la machine, vérifier l'espace du "
        "cliché, puis relancer la sauvegarde.",
    "Délai dépassé":
        "Vérifier la charge et le lien réseau de la source ; allonger la "
        "fenêtre si la sauvegarde est simplement trop longue.",
    "Fichier illisible ou inaccessible":
        "Repérer le fichier en cause (verrouillé, corrompu) et l'exclure ou "
        "le débloquer.",
    "Interrompu par l'opérateur":
        "Confirmer que l'arrêt était voulu ; sinon relancer la sauvegarde.",
    "Sauvegarde sans données":
        "Vérifier la sélection de la source : la tâche a tourné sans rien "
        "copier.",
}


def action_conseillee(libelle: str) -> str:
    """Quoi faire pour ce problème, quand il est reconnu. Chaîne vide si le
    problème n'est pas d'un cas connu — mieux vaut ne rien dire que dire une
    généralité."""
    return ACTIONS_CONNUES.get((libelle or "").split(" — ")[0].split(" (")[0],
                               "")


# Codes qui ne disent RIEN de la cause. Retrospect -1001 accompagne
# « MapError: unknown Windows error -1 » : c'est l'aveu d'ignorance du
# produit, vu sur 84 courriels du parc coiffant des incidents sans rapport.
CODES_NON_DIAGNOSTIQUES = {"-1001"}

_CODES_RX = [(re.compile(p), propre) for p, propre in PROBLEM_CODES]
_KNOWN_RX = [(re.compile(p), lib, grp) for p, lib, grp in PROBLEM_KNOWN]


# Noms de jour et de mois (déjà pliés par fold_text : minuscules, sans
# accents). Retirer les CHIFFRES ne suffit pas à stabiliser une date écrite
# en toutes lettres : « AZIMUTH - [Failed] ATELIER - Mascouche - Thursday,
# 30 July 2026 » et son jumeau du lundi 27 donnaient DEUX signatures — la
# même tâche Veeam en échec comptait deux problèmes distincts, chacun
# « depuis » sa propre date (rapport diagnostic du 2026-08-05). Le retrait
# est uniforme, donc sans effet sur le regroupement des textes sans date —
# y compris quand un mot de la liste est un mot ordinaire (« may », « mars »).
_MOTS_DATE = re.compile(
    r"\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre"
    r"|octobre|novembre|decembre"
    r"|january|february|march|april|may|june|july|august|september"
    r"|october|november|december)\b")


def _normalise_probleme(texte: str) -> str:
    """Réduit un texte de problème à ce qui ne varie pas d'un courriel à
    l'autre : ni chemin, ni GUID, ni nombre, ni date, ni ponctuation."""
    t = fold_text(texte)
    t = re.sub(r"[a-z]:\\\S*", " ", t)          # chemins Windows
    t = re.sub(r"\{[0-9a-f-]{8,}\}", " ", t)    # GUID
    t = re.sub(r"\b\d[\d.,:/-]*", " ", t)       # nombres, dates, tailles
    t = re.sub(r"[^a-z\s]+", " ", t)
    t = _MOTS_DATE.sub(" ", t)                  # jours et mois en lettres
    return " ".join(t.split()[:8])


def problem_signature(texte: str) -> tuple[str, str]:
    """(clé stable, libellé lisible) du problème décrit par ce texte.
    Clé vide si le texte ne dit rien d'exploitable."""
    texte = texte or ""
    code, code_propre = "", False
    for rx, propre in _CODES_RX:
        m = rx.search(texte)
        if m and (not code or (propre and not code_propre)):
            # Casse normalisée pour que la clé soit stable, mais le « 0x »
            # d'un HRESULT reste en minuscule : « Code 0X8004230F » se lisait
            # mal dans le tableau.
            brut = m.group(1)
            code = ("0x" + brut[2:].upper() if brut[:2].lower() == "0x"
                    else brut.upper())
            # Un code qui veut dire « erreur inconnue » ne diagnostique rien :
            # il ne doit ni coiffer un cas reconnu, ni regrouper des incidents
            # sans rapport — même règle que pour les HRESULT génériques.
            code_propre = propre and code not in CODES_NON_DIAGNOSTIQUES
            if code_propre:
                break
    # Cas connu retenu = celui qui apparaît le PLUS TÔT dans le texte. Le
    # texte étant centré sur le motif déclencheur, c'est le plus proche de la
    # cause ; l'ordre de la liste ne départage plus qu'à égalité de position,
    # sans quoi un motif général placé haut avalerait un cas précis situé
    # juste sous le déclencheur.
    # Un motif qui CAPTURE l'objet passe avant : sur « Repository
    # Uncontactable / Repository NAS-JANCOR has become uncontactable », le mot
    # nu de l'en-tête apparaît le premier, mais c'est la forme complète qui
    # sait distinguer NAS-JANCOR de NAS-RUSCIO.
    meilleur = None
    for rang, (rx, lib, grp) in enumerate(_KNOWN_RX):
        m = rx.search(texte)
        if not m:
            continue
        objet = (m.group(grp) or "").strip() if grp else ""
        candidat = (0 if objet else 1, m.start(), rang, lib, objet)
        if meilleur is None or candidat[:3] < meilleur[:3]:
            meilleur = candidat
    if code_propre:
        # Identifiant du produit : il fait signature à lui seul.
        return f"code:{code}", (f"{meilleur[3]} ({code})" if meilleur
                                else f"Code {code}")
    if meilleur:
        _, _, _, lib, objet = meilleur
        cle = "cas:" + fold_text(lib)[:48]
        if objet:
            cle += ":" + fold_text(objet)[:24]
        libelle = f"{lib} — {objet}" if objet else lib
        return cle, (f"{libelle} ({code})" if code else libelle)
    if code:
        return f"code:{code}", f"Code {code}"
    base = _normalise_probleme(texte)
    return (f"txt:{base}", base) if base else ("", "")


def _problem_after(text: str, m) -> str:
    """Texte À PARTIR du motif déclencheur. C'est lui qui sert de base à la
    signature textuelle : ce qui PRÉCÈDE le motif est le sujet et l'entête,
    quasi identiques d'un courriel à l'autre — ils noieraient la cause dans
    le budget de mots de _normalise_probleme."""
    return re.sub(r"\s+", " ", text[m.start():m.start() + 250]).strip()


# Sujets qui portent eux-mêmes le VERDICT du produit, sous forme de comptes.
# Ils priment sur tout ce que dit le corps : celui-ci est un JOURNAL, pas un
# verdict, et un journal de sauvegarde réussie contient des lignes d'échec.
# Cas réel du parc (2026-07-28) : « ProActive - Remote - Acco Loisirs -
# 0 erreurs, 0 avertissements - Retrospect » dont le corps détaille du bruit
# VSS (« … failed. Aborting writer exclude, osErr -1, error -1001 [*]
# MapError: unknown Windows error -1 ») — 84 courriels annonçant zéro erreur
# étaient comptés en panne. Pire : deux courriels au sujet identique
# tombaient de part et d'autre selon le bavardage de leur corps.
VERDICTS_SUJET = {
    "retrospect": [
        r"(?i)-\s*(?P<err>\d+)\s*erreurs?\s*,\s*(?P<avert>\d+)"
        r"\s*avertissements?\s*-",
        r"(?i)-\s*(?P<err>\d+)\s*errors?\s*,\s*(?P<avert>\d+)"
        r"\s*warnings?\s*-",
    ],
}
_VERDICTS_RX = {p: [re.compile(r) for r in rs]
                for p, rs in VERDICTS_SUJET.items()}

# Digests dont le sujet porte le verdict EN TOUTES LETTRES, quel que soit le
# produit détecté au contenu. Champ étroit : sujets complets observés [VU],
# jamais un mot isolé — un faux succès masquerait de vraies pannes.
# Sauvegarde infonuagique S3 (2026-07-29, tranché avec le parc réel) : le
# corps n'aligne que des compteurs de FICHIERS — « Failed: 140/17 » sur
# « Total: 725318/295 », soit 0,02 % de fichiers sautés — et le mot nu
# « failed » comptait 7 courriels « Successful » en panne. Le produit émet
# lui-même « Errors occurred » quand ça tourne mal : son sujet est le
# verdict, comme pour ProActive.
VERDICTS_SUJET_MOTS = [
    (r"(?i)^backups s3 successful\s*$", STATUS_SUCCESS),
    (r"(?i)^backups s3 errors occurred\s*$", STATUS_ERROR),
    # Forme FRANÇAISE du même produit (« Sauvegardes S3 » chez Vertika et
    # Seanautic, rapport diagnostic du 2026-08-05) : le sujet complet
    # observé porte le verdict. Sans lui, « 2 Avertissement importants »
    # du corps classait en avertissement un courriel dont le produit
    # annonce des ERREURS (« 1 Erreur très critique »). Le pendant succès
    # français n'a pas été observé — ne pas l'inventer.
    (r"(?i)^sauvegardes s3 des erreurs se sont produites\s*$", STATUS_ERROR),
    # vzdump (Proxmox VE) : même situation que S3 et que ProActive — le corps
    # est un JOURNAL d'exécution, où « ERROR: fs-freeze failed » côtoie
    # « finished successfully » dans une sauvegarde réussie. Le sujet, lui,
    # est le verdict du produit : « vzdump backup status (pve1) : backup
    # successful ». Gabarit lu dans le code source de pve-manager, donc forme
    # certaine ; le sujet complet est exigé, jamais un mot isolé.
    (r"(?i)^\s*(?:(?:re|tr|fwd?)\s*:\s*)*vzdump backup status[^\S\r\n]*"
     r"\([^)\r\n]*\)[^\S\r\n]*:[^\S\r\n]*backup successful\s*$", STATUS_SUCCESS),
    (r"(?i)^\s*(?:(?:re|tr|fwd?)\s*:\s*)*vzdump backup status[^\S\r\n]*"
     r"\([^)\r\n]*\)[^\S\r\n]*:[^\S\r\n]*backup failed\b", STATUS_ERROR),
]
_VERDICTS_MOTS_RX = [(re.compile(p), st) for p, st in VERDICTS_SUJET_MOTS]


def verdict_sujet(product: str, subject: str) -> tuple[str, str]:
    """(statut, motif) quand le SUJET annonce lui-même son résultat — par
    ses comptes (« 2 erreurs, 0 avertissements ») ou en toutes lettres
    (« Backups S3 Successful ») ; sinon ("", ""). Le corps n'est alors plus
    consulté pour le classement."""
    for rx, st in _VERDICTS_MOTS_RX:
        if rx.search(subject or ""):
            return st, rx.pattern
    for rx in _VERDICTS_RX.get(product) or []:
        m = rx.search(subject or "")
        if not m:
            continue
        if int(m.group("err")):
            return STATUS_ERROR, rx.pattern
        if int(m.group("avert")):
            return STATUS_WARNING, rx.pattern
        return STATUS_SUCCESS, rx.pattern
    return "", ""


def _classify_text(pats: dict, text: str) -> tuple[str, str, str, str]:
    """(statut, motif déclencheur, contexte du problème, texte à partir du
    motif). Les deux derniers ne sont remplis que pour les erreurs et les
    avertissements."""
    for key, st in _ORDER:
        for rx in pats.get(key) or []:
            m = rx.search(text)
            if m:
                if st == STATUS_SUCCESS:
                    return st, rx.pattern, "", ""
                return (st, rx.pattern, _problem_context(text, m),
                        _problem_after(text, m))
    return STATUS_UNKNOWN, "", "", ""


def classify(cfg: dict, mail: RawMail) -> BackupEvent:
    # Espaces horizontaux normalisés AVANT tout motif. Un en-tête plié
    # (RFC 2822) restitue sa tabulation dans le sujet : « Backup with
    # ␉Warnings » échappait à « backup with warnings » et retombait sur le
    # motif générique « warning :| » — le même incident sortait sous DEUX
    # signatures (rapport diagnostic du 2026-08-05). Les sauts de ligne,
    # eux, sont préservés : les motifs ancrés (« (?im)^err », « ^status: »)
    # et les gardes « [^\S\r\n] » comptent dessus.
    sujet = re.sub(r"[^\S\r\n]+", " ", mail.subject or "")
    corps = re.sub(r"[^\S\r\n]+", " ", mail.body or "")
    text_mail = f"{sujet}\n{corps}"
    # Dossiers « auto » (mode client_folders) : le produit n'est pas connu du
    # dossier, on le détecte au contenu. Sinon on garde celui du dossier.
    product = mail.product
    if product not in _known_products(cfg):
        product = _detect_product(cfg, f"{text_mail}\n{mail.attachments_text}")
    pats = _patterns_for(cfg, product)
    # Étage 1 — sujet + corps seulement : un « failed » historique dans un
    # journal joint (« failed to read sector, retry ok ») ne doit pas
    # renverser le verdict du courriel lui-même. Le filet générique couvre
    # les courriels d'autres systèmes (mode client_folders).
    status, matched, problem, apres = _classify_text(pats, text_mail)
    # Étage 0 — le sujet porte parfois le verdict chiffré du produit. Il prime
    # alors sur le corps, qui n'est qu'un journal. On garde le contexte trouvé
    # dans le corps quand le verdict est mauvais : il dit QUOI regarder.
    v_statut, v_motif = verdict_sujet(product, sujet)
    if v_statut:
        if v_statut == STATUS_SUCCESS:
            status, matched, problem, apres = v_statut, v_motif, "", ""
        else:
            status, matched = v_statut, (matched or v_motif)
    if status == STATUS_UNKNOWN:
        status, matched, problem, apres = _classify_text(_generic_compiled(),
                                                         text_mail)
    # Étage 2 — pièces jointes, seulement si le courriel seul reste inconnu
    # (rapports dont tout le contenu utile est dans la pièce jointe).
    if status == STATUS_UNKNOWN and mail.attachments_text:
        status, matched, problem, apres = _classify_text(
            pats, mail.attachments_text)
        if status == STATUS_UNKNOWN:
            status, matched, problem, apres = _classify_text(
                _generic_compiled(), mail.attachments_text)
    # Signature calculée d'abord sur le CONTEXTE du motif déclencheur : c'est
    # la phrase qui décrit l'incident, sans le pied de page ni le journal
    # complet qui feraient dériver la clé d'un envoi à l'autre. Si ce contexte
    # ne livre qu'un texte générique, on cherche un code ou un cas connu dans
    # tout le courriel — la vraie cause est souvent une ligne plus bas (« The
    # job failed with the following error: A communication failure… »).
    _signature = ("", "")
    if problem:
        # La signature se calcule à partir du déclencheur : ce qui le précède
        # est l'entête, commun à tous les courriels du même produit.
        _signature = problem_signature(apres or problem)
        if _signature[0].startswith("txt:"):
            # Contexte muet : la cause est souvent quelques lignes plus bas
            # (« The job failed with the following error: … »). On n'accepte
            # alors QUE les cas connus et les codes — jamais un autre texte
            # normalisé, qui serait pris n'importe où dans le courriel.
            large = problem_signature(text_mail)
            if large[0] and not large[0].startswith("txt:"):
                _signature = large
            elif product == "retrospect" and re.search(
                    r"(?i)notification[^\S\r\n]+d['’]erreur|error notification",
                    sujet):
                # Rien d'exploitable nulle part : le courriel annonce une
                # erreur sans la décrire. Signature stable et lisible (avec
                # action) plutôt qu'un texte normalisé de pied de page.
                _signature = ("cas:" + fold_text(LIB_NOTIF_SANS_DETAIL)[:48],
                              LIB_NOTIF_SANS_DETAIL)
            elif product == "sqlagent" and status == STATUS_ERROR:
                # Même situation côté SQL Server Agent, et elle concerne le
                # deuxième volume du parc (248 courriels, tous en succès à ce
                # jour — donc ce chemin-ci n'a jamais été vu en vrai). Quand
                # le corps ne porte AUCUNE cause reconnaissable, la signature
                # retombait sur le gabarit Microsoft lui-même :
                # « the job failed sql server job system sauvegarde » — du
                # charabia, et sans marche à suivre.
                #
                # Comme pour Retrospect ci-dessus, cette branche ne s'exécute
                # QUE si rien d'autre n'a été trouvé : une vraie cause dans
                # « MESSAGES: » garde toujours la priorité. Le nom du travail
                # entre dans la clé (et non dans le libellé) pour que deux
                # travaux distincts restent deux incidents distincts.
                travail = _extract(pats["extract"].get("job"), sujet,
                                   mail.body)
                cle = "cas:" + fold_text(LIB_SQL_SANS_DETAIL)[:48]
                if travail:
                    cle += ":" + fold_text(travail)[:24]
                _signature = (cle, LIB_SQL_SANS_DETAIL)
    return BackupEvent(
        product=product,
        status=status,
        subject=mail.subject,
        sender=mail.sender,
        received=mail.received,
        folder=mail.folder,
        machine=_extract(pats["extract"].get("machine"), sujet,
                         corps, mail.attachments_text),
        job=_extract(pats["extract"].get("job"), sujet, corps,
                     mail.attachments_text),
        matched_pattern=matched,
        problem=problem,
        problem_key=_signature[0],
        problem_label=_signature[1],
        # Borné à 5000 caractères AVANT la normalisation : l'extrait affiché
        # fait 500 caractères, inutile de normaliser un corps de 200 Ko.
        excerpt=re.sub(r"\s+", " ", mail.body[:5000]).strip()[:500],
        attachments_note=mail.attachments_note,
        # Priorité : dossier client (mode client_folders) > client extrait du
        # sujet/corps (ex. nomenclature ProActive de Retrospect) > section
        # « clients » de config.yaml (appliquée ensuite par analyze()).
        client=mail.client or _extract(pats["extract"].get("client"),
                                       sujet, corps),
    )


# ─── jonction avec BGCourriel ─────────────────────────────────────────────
# Tout ce qui précède est le moteur de backup-monitor, repris tel quel. Ce
# qui suit est propre à BGCourriel : ça lit le corps complet À LA DEMANDE
# (corps.py) pour les seuls courriels déjà repérés « sauvegarde » par
# classement.py, et ça pose le résultat sur le courriel — jamais l'inverse,
# jamais un balayage de toute la boîte.

# Priorité de la file d'action : un échec passe devant tout sauf un courriel
# marqué à la main. Un avertissement reste sous « sans réponse »/« à
# répondre » — personne n'a explicitement demandé de geste, contrairement à
# un humain qui attend une réponse.
ACTION_ERREUR = "sauvegarde en erreur"
ACTION_AVERTISSEMENT = "sauvegarde en avertissement"


def enrichir(courriels, chemins_mbox, maintenant=None):
    """Pour chaque courriel déjà classé « sauvegarde » (règle sur le sujet,
    voir classement.py), lit son corps complet et détermine erreur /
    avertissement / succès / inconnu. Une erreur ou un avertissement prend
    la file d'action, LU ou non — contrairement au reste de BGCourriel, une
    notification de sauvegarde qui a déjà été ouverte n'est pas réglée pour
    autant : elle reste un échec tant qu'une réussite ne l'a pas remplacée.

    `chemins_mbox` : {(compte, dossier): chemin du fichier mbox}, construit
    par etat.collecter() pendant la lecture des dossiers — un courriel dont
    le dossier n'y figure plus (déplacé entre l'index et cette lecture) est
    simplement laissé tel quel, sans corps complet."""
    maintenant_dt = (datetime.fromtimestamp(maintenant, tz=timezone.utc)
                     if maintenant is not None else datetime.now(timezone.utc))
    for courriel in courriels:
        if courriel.get("categorie") != "sauvegarde":
            continue
        chemin = chemins_mbox.get((courriel["compte"], courriel["dossier"]))
        if not chemin:
            continue
        corps_texte = corps.corps_complet(chemin, courriel.get("decalage_mbox", 0))
        if not corps_texte:
            continue
        mail = RawMail(
            subject=courriel.get("objet", ""),
            sender=courriel.get("expediteur", ""),
            received=maintenant_dt,
            body=corps_texte,
            folder=courriel.get("dossier", ""),
            product="auto",
        )
        evenement = classify({}, mail)
        courriel["statutSauvegarde"] = evenement.status
        courriel["machineSauvegarde"] = evenement.machine
        if evenement.status in (STATUS_ERROR, STATUS_WARNING):
            courriel["problemeSauvegarde"] = (
                evenement.problem_label or evenement.problem)
            action_faire = action_conseillee(evenement.problem_label)
            if action_faire:
                courriel["actionConseilleeSauvegarde"] = action_faire
            # Remplace l'action générique (souvent « à voir », ou rien si
            # déjà lu) : un échec de sauvegarde n'attend pas qu'on l'ouvre
            # pour rester un échec.
            courriel["action"] = (ACTION_ERREUR if evenement.status
                                  == STATUS_ERROR else ACTION_AVERTISSEMENT)

