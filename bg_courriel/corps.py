"""Lecture À LA DEMANDE du corps complet d'UN courriel dans le mbox, repéré
par son storeToken — l'octet où Thunderbird l'a écrit, donné par le .msf.
Jamais le reste du mbox n'est lu.

POURQUOI CE MODULE EXISTE. Le .msf ne porte qu'un aperçu du corps (quelques
centaines de caractères, voir mork.py) : suffisant pour trier une demande
d'une infolettre, pas pour retrouver un motif d'erreur enfoui plus bas dans
un rapport Macrium ou Retrospect (sauvegardes.py). Relire le mbox ENTIER à
chaque relève casserait la promesse de performance de BGCourriel (178 Ko
contre 30 Mo, voir mork.py) — storeToken donne l'octet exact, un seek suffit,
et ce module n'est appelé que pour les courriels déjà repérés « sauvegarde »
par classement.py (une poignée par relève, pas toute la boîte).

LECTURE SEULE. Le fichier est ouvert en « rb », jamais écrit, jamais copié :
un seek + une lecture courte ne risque pas de tomber sur une transaction
Thunderbird à moitié écrite comme le ferait une lecture du fichier entier.
"""

import email
import html as html_mod
import re
from email import policy


def lire_corps_brut(chemin_mbox: str, offset: int) -> bytes:
    """Les octets RFC822 d'UN courriel, depuis `offset` (storeToken) jusqu'à
    la prochaine ligne séparatrice « From <espace> » (exclue) ou la fin du
    fichier.

    Rend b"" si le fichier ou l'octet ont disparu (dossier déplacé, boîte
    purgée entre l'index et la lecture) : jamais d'exception qui ferait
    tomber toute la relève pour un seul courriel."""
    morceaux = []
    try:
        with open(chemin_mbox, "rb") as f:
            f.seek(offset)
            premiere = True
            for ligne in f:
                if not premiere and ligne.startswith(b"From "):
                    break
                morceaux.append(ligne)
                premiere = False
    except OSError:
        return b""
    brut = b"".join(morceaux)
    saut = brut.find(b"\n")
    brut = brut[saut + 1:] if saut != -1 else b""
    # Thunderbird écrit une SECONDE copie échappée de la ligne séparatrice
    # (« >From - <date> ») comme premier « en-tête » du courriel — sans ce
    # retrait, le module email ne reconnaît AUCUN en-tête (mesuré sur un
    # vrai profil : 258/258 courriels illisibles sans ce correctif ; même
    # piège que backup_monitor.fetch_thunderbird, même cause).
    if brut.startswith(b">From "):
        saut = brut.find(b"\n")
        brut = brut[saut + 1:] if saut != -1 else b""
    return brut


def _html_vers_texte(texte: str) -> str:
    texte = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", texte,
                   flags=re.S | re.I)
    texte = re.sub(r"<br\s*/?>|</p>|</tr>|</div>|</li>", "\n", texte,
                   flags=re.I)
    texte = re.sub(r"<[^>]+>", " ", texte)
    return html_mod.unescape(texte)


def texte_corps(brut: bytes) -> str:
    """Le texte du corps (HTML réduit à son texte, jamais rendu) d'un
    courriel brut. Chaîne vide si `brut` est vide ou illisible."""
    if not brut:
        return ""
    try:
        msg = email.message_from_bytes(brut, policy=policy.default)
        partie = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        return ""
    if partie is None:
        return ""
    try:
        texte = partie.get_content()
    except Exception:
        return ""
    if partie.get_content_type() == "text/html":
        texte = _html_vers_texte(texte)
    return texte


def corps_complet(chemin_mbox: str, offset: int) -> str:
    """Le texte complet du corps d'un courriel — l'unique point d'entrée du
    module pour le reste de l'outil."""
    return texte_corps(lire_corps_brut(chemin_mbox, offset))
