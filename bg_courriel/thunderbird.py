"""Découverte du profil Thunderbird : comptes, dossiers, fichiers d'index.

RIEN N'EST CONFIGURÉ À LA MAIN, ET C'EST VOULU. Les boîtes viennent du profil
lui-même. Une boîte ajoutée dans Thunderbird apparaît au tableau de bord à la
relève suivante sans qu'on touche à un fichier de configuration — c'est la
demande d'origine (« l'ensemble des boîtes »), et une liste écrite en dur
aurait vieilli dès le premier compte ajouté.

LECTURE SEULE, DE BOUT EN BOUT. Le profil est ouvert en « rb » et jamais
autrement. Deux précautions s'ajoutent à la bonne intention :

  1. Le .msf est COPIÉ avant d'être analysé. Thunderbird écrit ses index par
     ajout, sans verrou : lire pendant qu'il écrit donnerait un fichier tronqué
     au milieu d'une transaction. La copie prend quelques millisecondes sur
     178 Ko et supprime la question.

  2. Aucun appel à Thunderbird, aucun signal, aucun greffon. Le programme
     fonctionne que Thunderbird soit ouvert ou fermé.

LE DÉCALAGE, DIT FRANCHEMENT. Thunderbird garde son index en mémoire et ne
l'écrit sur disque que de temps à autre (changement de dossier, fermeture,
purge périodique). Quand il est ouvert, le tableau de bord peut donc retarder
de quelques minutes sur ce que l'écran affiche. C'est le prix de ne dépendre
d'aucun mot de passe et d'aucune extension ; horodater la relève avec la date
de modification du .msf permet au moins de savoir de quand datent les chiffres
(voir champ « fraicheur » plus bas).
"""

import base64
import configparser
import os
import re
import shutil
import tempfile
import unicodedata

from . import mork

_SUFFIXE_DOUBLON = re.compile(r"-\d+$")


def decoder_nom_imap(nom):
    """Décode l'UTF-7 modifié dans lequel IMAP écrit les noms de dossiers.

    Thunderbird nomme ses fichiers d'index comme le serveur nomme ses dossiers,
    et IMAP (RFC 3501 § 5.1.3) encode tout ce qui n'est pas ASCII dans un UTF-7
    à lui. « Éléments envoyés » arrive donc sur le disque comme
    « &AMk-l&AOk-ments envoy&AOk-s », et « Tâches » comme « T&AOI-ches ».

    Deux conséquences, et la seconde a été mesurée : c'est illisible à l'écran,
    et surtout AUCUNE comparaison de nom ne fonctionne — la liste des dossiers à
    écarter ne reconnaissait pas « Courrier ind&AOk-sirable » comme du courrier
    indésirable, et 16 indésirables sont entrés dans les totaux.

    Le codec « utf-7 » de Python NE convient PAS : la variante d'IMAP remplace le
    « / » du base64 par une virgule et se termine par un tiret. D'où ce décodage
    à la main, qui rend le nom tel quel si la séquence est illisible plutôt que
    de lever une exception au milieu d'une relève.
    """
    if "&" not in nom:
        return nom
    morceaux = []
    i = 0
    while i < len(nom):
        if nom[i] != "&":
            morceaux.append(nom[i])
            i += 1
            continue
        fin = nom.find("-", i + 1)
        if fin < 0:
            morceaux.append(nom[i:])
            break
        contenu = nom[i + 1:fin]
        if not contenu:
            morceaux.append("&")                    # « &- » est un & littéral
        else:
            b64 = contenu.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                morceaux.append(base64.b64decode(b64).decode("utf-16-be"))
            except (ValueError, UnicodeDecodeError):
                morceaux.append(nom[i:fin + 1])     # illisible : tel quel
        i = fin + 1
    return "".join(morceaux)


def normaliser_nom(nom):
    """La forme sur laquelle on compare : sans accents, sans casse, sans doublon.

    Le suffixe « -1 » est retiré parce que Thunderbird le colle au nom quand un
    fichier d'index existe déjà : « Brouillons-1 » est bien le dossier des
    brouillons, et une liste qui ne connaît que « Brouillons » le laisserait
    passer. Les accents sautent pour la même raison — comparer « Éléments » à
    « elements » ne doit pas dépendre d'un accent.
    """
    decode = decoder_nom_imap(nom)
    sans_doublon = _SUFFIXE_DOUBLON.sub("", decode)
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", sans_doublon)
        if unicodedata.category(c) != "Mn")
    return sans_accent.casefold().strip()

RACINES = [
    os.path.expanduser("~/.thunderbird"),
    os.path.expanduser("~/.mozilla-thunderbird"),           # profils très anciens
    os.path.expanduser("~/snap/thunderbird/common/.thunderbird"),
    os.path.expanduser("~/.var/app/org.mozilla.Thunderbird/.thunderbird"),
]

# ── dossiers écartés du tableau de bord ─────────────────────────────────────
#
# CETTE LISTE A ÉTÉ REFAITE APRÈS QUE THUNDERBIRD A SYNCHRONISÉ TOUTE
# L'ARBORESCENCE OFFICE 365. Au départ, un seul dossier existait par compte
# (INBOX) et une courte liste anglaise suffisait. Quand les 35 dossiers réels
# sont apparus, la liste a laissé passer 8 éléments supprimés, 16 indésirables
# (dont 13 « non lus ») et 3 brouillons : le total est passé de 305 à 342
# courriels et le non-lu de 234 à 255, sans qu'aucun courriel ne soit arrivé.
#
# Trois raisons d'écarter, et elles ne se valent pas :
#
#   ce qui n'est pas du courrier REÇU — envoyés, brouillons, boîte d'envoi.
#     Les compter fausse deux chiffres à la fois : un brouillon porte le drapeau
#     « non lu » et entrerait dans la file d'action alors que personne ne l'a
#     jamais envoyé.
#   ce qui est déjà jugé — corbeille, éléments supprimés, indésirables. Leur
#     contenu n'attend plus rien.
#   ce qui n'est pas du courrier du tout — Exchange expose en IMAP son
#     calendrier, ses contacts, ses tâches, ses notes et ses dossiers de
#     conflits de synchronisation. Ils sont vides de courriels et le resteront.
#
# « Archive » est volontairement ABSENT de la liste : c'est du vrai courrier reçu,
# rangé. Il a sa place dans la volumétrie et dans l'historique.
DOSSIERS_IGNORES = {
    # pas du courrier reçu
    "sent", "sent items", "elements envoyes", "envoyes", "messages envoyes",
    "drafts", "brouillons",
    "outbox", "boite d'envoi", "unsent messages", "messages en attente",
    "templates", "modeles",
    # déjà jugé
    "trash", "corbeille", "deleted items", "elements supprimes", "supprimes",
    "junk", "junk e-mail", "spam", "courrier indesirable", "indesirables",
    # pas du courrier
    "calendar", "calendrier", "contacts", "tasks", "taches", "notes",
    "journal", "rss feeds", "flux rss",
    "conversation history", "historique des conversations",
    "sync issues", "problemes de synchronisation",
}


def trouver_profil(racines=None):
    """Le profil par défaut, ou None si Thunderbird n'est pas installé.

    profiles.ini reste la seule source qui dise QUEL profil est le bon quand il
    y en a plusieurs — ici « 7ekkuigg.default-default » et « z9s5qkkc.default »
    cohabitent, et deviner par la date de modification se tromperait un jour.
    """
    for racine in racines or RACINES:
        ini = os.path.join(racine, "profiles.ini")
        if not os.path.exists(ini):
            continue
        cfg = configparser.RawConfigParser()
        cfg.read(ini, encoding="utf-8")
        # Une section [Install…] nomme le profil réellement lancé ; elle prime
        # sur l'attribut Default=1, qui peut rester sur un profil abandonné.
        for section in cfg.sections():
            if section.startswith("Install") and cfg.has_option(section, "Default"):
                chemin = cfg.get(section, "Default")
                complet = chemin if os.path.isabs(chemin) else os.path.join(racine, chemin)
                if os.path.isdir(complet):
                    return complet
        for section in cfg.sections():
            if not section.startswith("Profile"):
                continue
            if cfg.get(section, "Default", fallback="0") != "1":
                continue
            chemin = cfg.get(section, "Path", fallback="")
            relatif = cfg.get(section, "IsRelative", fallback="1") == "1"
            complet = os.path.join(racine, chemin) if relatif else chemin
            if os.path.isdir(complet):
                return complet
    return None


def lire_prefs(profil):
    """prefs.js réduit à un dictionnaire clé -> valeur.

    Le fichier est du JavaScript, mais d'une régularité totale : une ligne
    « user_pref("clé", valeur); » par préférence. On ne lit que ce qui commence
    par « mail. » : le reste (fenêtres, polices, greffons) ne nous concerne pas
    et représente l'essentiel du fichier.
    """
    prefs = {}
    motif = re.compile(r'user_pref\("(mail\.[^"]+)",\s*(.*?)\);\s*$')
    chemin = os.path.join(profil, "prefs.js")
    if not os.path.exists(chemin):
        return prefs
    with open(chemin, encoding="utf-8", errors="replace") as f:
        for ligne in f:
            m = motif.match(ligne.strip())
            if not m:
                continue
            cle, brut = m.group(1), m.group(2).strip()
            if brut.startswith('"') and brut.endswith('"'):
                valeur = brut[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            elif brut in ("true", "false"):
                valeur = brut == "true"
            else:
                try:
                    valeur = int(brut)
                except ValueError:
                    valeur = brut
            prefs[cle] = valeur
    return prefs


def comptes(profil, prefs=None):
    """Les comptes du profil, dans l'ordre où Thunderbird les affiche.

    Un compte relie trois objets de prefs.js : le compte (accountN), son
    serveur (serverN, qui porte le dossier sur disque) et son identité (idN,
    qui porte l'adresse. Un compte sans identité — les dossiers locaux — n'en a
    pas, et ce n'est pas une anomalie).
    """
    prefs = prefs if prefs is not None else lire_prefs(profil)
    liste = []
    noms = str(prefs.get("mail.accountmanager.accounts", "") or "")
    for cle in [c.strip() for c in noms.split(",") if c.strip()]:
        serveur = prefs.get(f"mail.account.{cle}.server")
        if not serveur:
            continue
        genre = prefs.get(f"mail.server.{serveur}.type", "")
        dossier = prefs.get(f"mail.server.{serveur}.directory", "")
        if not dossier:
            # directory-rel : « [ProfD]ImapMail/… » relatif au profil
            rel = str(prefs.get(f"mail.server.{serveur}.directory-rel", "") or "")
            if rel.startswith("[ProfD]"):
                dossier = os.path.join(profil, rel[len("[ProfD]"):])
        identites = str(prefs.get(f"mail.account.{cle}.identities", "") or "")
        premiere = identites.split(",")[0].strip()
        adresse = prefs.get(f"mail.identity.{premiere}.useremail", "") if premiere else ""
        if not adresse:
            # Un compte sans identité — les dossiers locaux — n'a pas d'adresse.
            # Son nom de serveur (« Local Folders ») est ce que Thunderbird
            # affiche ; se rabattre sur userName donnerait « nobody », qui ne
            # veut rien dire pour qui lit le tableau.
            adresse = (prefs.get(f"mail.server.{serveur}.name", "")
                       or prefs.get(f"mail.server.{serveur}.userName", ""))
        liste.append({
            "cle": cle,
            "genre": genre,                                    # imap, pop3, none
            "adresse": adresse,
            "nom": prefs.get(f"mail.server.{serveur}.name", "") or adresse,
            "serveur": prefs.get(f"mail.server.{serveur}.hostname", ""),
            "dossier": dossier,
            "nom_affiche": prefs.get(f"mail.identity.{premiere}.fullName", "") if premiere else "",
        })
    return liste


def dossiers(compte, avec_ignores=False):
    """Les dossiers d'un compte : un .msf trouvé = un dossier.

    Les sous-dossiers vivent dans des répertoires « Nom.sbd », récursivement.
    On descend, parce qu'un classement par client — le cas de figure d'origine
    de l'ancien outil — est fait de sous-dossiers.
    """
    racine = compte.get("dossier") or ""
    trouves = []
    if not racine or not os.path.isdir(racine):
        return trouves

    def descendre(repertoire, prefixe):
        try:
            entrees = sorted(os.listdir(repertoire))
        except OSError:
            return
        for entree in entrees:
            complet = os.path.join(repertoire, entree)
            if entree.endswith(".msf") and os.path.isfile(complet):
                brut = entree[:-4]
                if not avec_ignores and normaliser_nom(brut) in DOSSIERS_IGNORES:
                    continue
                nom = decoder_nom_imap(brut)
                trouves.append({
                    "nom": nom,
                    "nom_fichier": brut,
                    # Nom de rapprochement : sert à repérer deux fichiers d'index
                    # qui désignent le même dossier (« Archive » et
                    # « Archive-1 »), pour ne pas compter son courrier deux fois.
                    "cle": ((prefixe + "/") if prefixe else "") + normaliser_nom(brut),
                    "chemin_affiche": (prefixe + "/" + nom) if prefixe else nom,
                    "msf": complet,
                    "fraicheur": int(os.path.getmtime(complet)),
                })
            elif entree.endswith(".sbd") and os.path.isdir(complet):
                brut = entree[:-4]
                # Un sous-dossier d'un dossier écarté est écarté avec lui : les
                # « Conflits » et « Échecs locaux » vivent sous « Problèmes de
                # synchronisation », et n'ont pas plus de raison d'être comptés
                # que leur parent.
                if not avec_ignores and normaliser_nom(brut) in DOSSIERS_IGNORES:
                    continue
                sous = decoder_nom_imap(brut)
                descendre(complet, (prefixe + "/" + sous) if prefixe else sous)

    descendre(racine, "")
    return trouves


def lire_index(chemin_msf):
    """Analyse un .msf par l'intermédiaire d'une copie.

    La copie n'est pas de la prudence gratuite : Thunderbird ajoute ses
    transactions en fin de fichier sans verrou, et une lecture concurrente peut
    tomber au milieu d'une transaction incomplète. Analyser une copie fige
    l'instant lu.
    """
    with tempfile.NamedTemporaryFile(suffix=".msf", delete=False) as tmp:
        copie = tmp.name
    try:
        shutil.copy2(chemin_msf, copie)
        return mork.analyser_fichier(copie)
    finally:
        try:
            os.unlink(copie)
        except OSError:
            pass


def inventaire(profil=None):
    """Tout le profil d'un coup : comptes et dossiers, sans analyser les index.

    Sert à la commande « dossiers » : montrer ce que l'outil voit avant de lui
    demander de compter quoi que ce soit.
    """
    profil = profil or trouver_profil()
    if not profil:
        return {"profil": None, "comptes": []}
    prefs = lire_prefs(profil)
    resultat = {"profil": profil, "comptes": []}
    for compte in comptes(profil, prefs):
        entree = dict(compte)
        entree["dossiers"] = dossiers(compte)
        resultat["comptes"].append(entree)
    return resultat
