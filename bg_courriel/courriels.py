"""Des lignes Mork aux courriels : décodage, drapeaux, fils de discussion.

Le lecteur Mork rend des cellules brutes — « 81 » pour les drapeaux,
« =?UTF-8?q?Nous_avons_re=C3=A7u?= » pour un objet, « 1|TELUS <…> » pour un
expéditeur. Ce module en fait des courriels lisibles, et il est le SEUL endroit
où ces bizarreries sont connues : tout ce qui vient après (tri, tendances,
affichage) travaille sur des champs propres.

TROIS BIZARRERIES QUI ONT COÛTÉ UN ALLER-RETOUR

1. L'objet n'est PAS toujours décodé dans le .msf. Thunderbird y recopie
   parfois l'en-tête MIME telle quelle : « =?utf-8?B?8J+OiSBOZXcgQ291cnNl…?= ».
   Un tableau de bord qui affiche ça est illisible. On décode donc en RFC 2047,
   ce qui est sans effet sur les objets déjà en clair.

2. « sender_name » porte un chiffre et une barre en tête — « 1|TELUS <…> ».
   Ce chiffre est un marqueur interne de Thunderbird, pas une partie du nom.

3. Le fil de discussion se lit sur « msgThreadId », pas sur « threadId » :
   cette dernière colonne existe dans le dictionnaire du fichier mais n'est
   remplie que pour les lignes de fil, pas pour les courriels. Chercher au
   mauvais endroit rend un fil vide pour tout le monde, sans erreur visible.
"""

import email.header
import email.utils
import re

from . import mork

_PREFIXE_NOM = re.compile(r"^\d+\|")
_RE_PREFIXE = re.compile(r"^\s*((re|ré|fw|fwd|tr|rép)\s*(\[\d+\])?\s*:\s*)+", re.I)


def _entier(valeur, defaut=0):
    """Les nombres du .msf sont en hexadécimal, sans préfixe."""
    try:
        return int(str(valeur or "0"), 16)
    except (ValueError, TypeError):
        return defaut


def _entier_decimal(valeur, defaut=0):
    """storeToken fait exception : c'est un décalage d'octet dans le mbox,
    écrit en DÉCIMAL — pas en hexadécimal comme les autres nombres du .msf.
    Vérifié à la main (offset lu ici, seek au même octet dans le vrai mbox :
    tombe exactement sur la ligne « From - <date> » du courriel)."""
    try:
        return int(str(valeur or "0"))
    except (ValueError, TypeError):
        return defaut


def _decoder_entete(brut):
    """Décode un en-tête RFC 2047 ; rend le texte tel quel s'il est en clair."""
    if not brut:
        return ""
    if "=?" not in brut:
        return brut.strip()
    morceaux = []
    try:
        for texte, codage in email.header.decode_header(brut):
            if isinstance(texte, bytes):
                try:
                    morceaux.append(texte.decode(codage or "utf-8", "replace"))
                except (LookupError, UnicodeDecodeError):
                    morceaux.append(texte.decode("latin-1", "replace"))
            else:
                morceaux.append(texte)
    except (email.errors.HeaderParseError, ValueError):
        return brut.strip()
    return "".join(morceaux).strip()


def _adresse(brut):
    """Rend (nom affiché, adresse en minuscules) à partir d'un en-tête d'adresse."""
    if not brut:
        return "", ""
    nom, adresse = email.utils.parseaddr(_decoder_entete(_PREFIXE_NOM.sub("", brut)))
    return nom.strip().strip('"'), adresse.strip().lower()


def _adresses(brut):
    """Toutes les adresses d'un en-tête « À » ou « Copie »."""
    if not brut:
        return []
    texte = _decoder_entete(brut)
    return [a.strip().lower() for _, a in email.utils.getaddresses([texte]) if a.strip()]


def sujet_normalise(objet):
    """L'objet sans les « Re: » et « Tr: » empilés — pour regrouper un fil.

    Sert de repli quand msgThreadId manque : deux courriels qui partagent un
    objet normalisé et un correspondant appartiennent visiblement au même
    échange, même si Thunderbird ne les a pas cousus.
    """
    return _RE_PREFIXE.sub("", objet or "").strip().lower()


def convertir(cellules, compte, dossier):
    """Une ligne Mork -> un courriel propre.

    « compte » et « dossier » sont recopiés dans le courriel : après le
    rassemblement de plusieurs boîtes, un courriel doit pouvoir dire d'où il
    vient sans qu'on ait à le retrouver.
    """
    drapeaux = _entier(cellules.get("flags"))
    nom_exp, adr_exp = _adresse(cellules.get("sender") or cellules.get("sender_name"))
    destinataires = _adresses(cellules.get("recipients"))
    objet = _decoder_entete(cellules.get("subject"))
    # date : la date d'envoi annoncée par l'expéditeur ; dateReceived : celle de
    # l'arrivée. La seconde ne ment pas sur l'ordre d'arrivée, la première est
    # celle que l'œil attend. On garde les deux, on trie sur la réception quand
    # elle existe.
    envoye = _entier(cellules.get("date"))
    recu = _entier(cellules.get("dateReceived")) or envoye
    return {
        "compte": compte,
        "dossier": dossier,
        "objet": objet,
        "sujet_fil": sujet_normalise(objet),
        "expediteur": adr_exp,
        "expediteur_nom": nom_exp or adr_exp,
        "domaine": adr_exp.rsplit("@", 1)[-1] if "@" in adr_exp else "",
        "destinataires": destinataires,
        "envoye": envoye,
        "recu": recu,
        "taille": _entier(cellules.get("size")),
        # Décalage dans le mbox (voir corps.py) : ne sert qu'aux courriels
        # repérés « sauvegarde », pour lire leur corps complet à la demande
        # sans jamais relire tout le mbox.
        "decalage_mbox": _entier_decimal(cellules.get("storeToken")),
        "apercu": (cellules.get("preview") or "").strip(),
        "message_id": (cellules.get("message-id") or "").strip(),
        "fil": (cellules.get("msgThreadId") or "").strip(),
        "parent_fil": (cellules.get("threadParent") or "").strip(),
        "etiquettes": [e for e in (cellules.get("keywords") or "").split() if e],
        "lu": bool(drapeaux & mork.LU),
        "repondu": bool(drapeaux & mork.REPONDU),
        "marque": bool(drapeaux & mork.MARQUE),
        "transfere": bool(drapeaux & mork.TRANSFERE),
        "surveille": bool(drapeaux & mork.SURVEILLE),
        "ignore": bool(drapeaux & mork.IGNORE),
        "drapeaux": drapeaux,
    }


def vivant(courriel):
    """Faux pour un courriel effacé dont la ligne survit dans l'index.

    Une ligne Mork n'est pas retirée quand le courriel est supprimé : elle
    reçoit un drapeau. Compter sans ce filtre gonflerait les totaux de tous les
    courriels jamais reçus depuis la création de la boîte.
    """
    return not courriel["drapeaux"] & (mork.EXPURGE | mork.EFFACE_IMAP)


def lire_dossier(base, compte, dossier):
    """Les courriels vivants d'un dossier, du plus récent au plus ancien."""
    lignes = base.lignes_de_portee(mork.PORTEE_COURRIELS)
    # Appartenance à la table des courriels du dossier, quand elle existe : c'est
    # la liste dont Thunderbird se sert lui-même. Les deux critères concordent
    # sur les boîtes mesurées ; garder les deux protège d'un index à moitié
    # réécrit, où une ligne subsiste sans être membre de rien.
    membres = None
    for infos in base.tables.values():
        if infos.get("genre") == mork.GENRE_COURRIELS:
            membres = set(infos.get("membres") or [])
            break
    resultat = []
    for rid, cellules in lignes.items():
        if membres is not None and rid not in membres:
            continue
        courriel = convertir(cellules, compte, dossier)
        if vivant(courriel):
            courriel["id"] = f"{compte}:{dossier}:{rid}"
            resultat.append(courriel)
    resultat.sort(key=lambda c: c["recu"], reverse=True)
    return resultat


def entete_dossier(base):
    """Les compteurs que Thunderbird tient lui-même, pour se contrôler.

    numMsgs et numNewMsgs (mal nommée : c'est le nombre de NON LUS) sont écrits
    par Thunderbird dans l'index. Les comparer à notre propre comptage est le
    meilleur autotest possible : il ne dépend d'aucune valeur codée en dur et
    reste valable quand les boîtes changent.
    """
    for cellules in base.lignes_de_portee(mork.PORTEE_ENTETE).values():
        return {
            "total_annonce": _entier(cellules.get("numMsgs")),
            "non_lus_annonce": _entier(cellules.get("numNewMsgs")),
            "nom_boite": cellules.get("mailboxName") or "",
        }
    return {"total_annonce": 0, "non_lus_annonce": 0, "nom_boite": ""}
