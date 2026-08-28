"""Envoi du document vers Firestore, sans dépendance hors bibliothèque standard.

MÊME APPROCHE QUE Claude_Lanceur/lanceur.py, ET POUR LES MÊMES RAISONS. Le
jeton du compte de service est fabriqué à la main — en-tête et corps en base64,
signature RS256 par la commande openssl — parce que le SDK Google traînerait
une centaine de paquets sur une machine où ce programme doit démarrer au
réveil, sans réseau garanti et sans environnement virtuel à entretenir.
L'approche est déjà en production ici depuis des semaines.

L'ACCÈS SERVEUR IGNORE LES RÈGLES FIRESTORE. Le compte de service écrit sous
users/<uid>/courriel/ sans que les règles s'appliquent — elles ne gouvernent
que les navigateurs. Ce qui protège le document, c'est donc, d'un côté, le
fichier de clé lisible du seul propriétaire de la machine, et de l'autre les
règles qui interdisent au navigateur tout ce qui n'est pas le compte connecté
du propriétaire.

CE PROGRAMME N'ÉCRIT QUE SON PROPRE DOCUMENT. Un seul chemin, écrit ici :
users/<uid>/courriel/etat. Il ne lit ni n'écrit rien d'autre — ni les
lancements, ni le marketing, ni BGFoods.
"""

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = os.path.expanduser("~/.config/bg-courriel/config.json")
CONFIG_LANCEUR = os.path.expanduser("~/.config/bg-lanceur/config.json")
CLE_LANCEUR = os.path.expanduser("~/.config/bg-lanceur/cle-sa.json")

COLLECTION = "courriel"
DOCUMENT = "etat"

_jeton = {"valeur": None, "expire": 0}


def charger_config():
    """La configuration, avec repli sur celle du lanceur déjà en place.

    Le projet Firestore, l'identifiant du propriétaire et la clé du compte de
    service sont les mêmes que pour le lanceur : les redemander dans un
    deuxième fichier inviterait à ce que les deux divergent un jour. On lit donc
    ~/.config/bg-courriel/config.json s'il existe, et sinon on emprunte au
    lanceur — en le disant dans le diagnostic.
    """
    config = {"projet": "", "uid": "", "cle_sa": "", "source": ""}
    if os.path.exists(CONFIG):
        with open(CONFIG, encoding="utf-8") as f:
            config.update(json.load(f))
        config["source"] = CONFIG
    elif os.path.exists(CONFIG_LANCEUR):
        with open(CONFIG_LANCEUR, encoding="utf-8") as f:
            emprunte = json.load(f)
        config["projet"] = emprunte.get("projet", "")
        config["uid"] = emprunte.get("uid", "")
        config["cle_sa"] = CLE_LANCEUR
        config["source"] = f"{CONFIG_LANCEUR} (emprunté)"
    config["cle_sa"] = os.path.expanduser(config.get("cle_sa") or CLE_LANCEUR)
    return config


def base_url(config):
    return (f"https://firestore.googleapis.com/v1/projects/{config['projet']}"
            f"/databases/(default)/documents")


def _b64url(donnees):
    return base64.urlsafe_b64encode(donnees).rstrip(b"=").decode()


def jeton_acces(config):
    """Un jeton d'accès Google, mis en cache jusqu'à deux minutes de sa fin."""
    if _jeton["valeur"] and time.time() < _jeton["expire"] - 120:
        return _jeton["valeur"]
    with open(config["cle_sa"], encoding="utf-8") as f:
        sa = json.load(f)
    maintenant = int(time.time())
    entete = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    corps = _b64url(json.dumps({
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/datastore",
        "aud": "https://oauth2.googleapis.com/token",
        "iat": maintenant, "exp": maintenant + 3600,
    }).encode())
    a_signer = f"{entete}.{corps}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as cle:
        cle.write(sa["private_key"])
        cle.flush()
        signature = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", cle.name],
            input=a_signer, capture_output=True, check=True).stdout
    jwt = f"{entete}.{corps}.{_b64url(signature)}"
    donnees = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt}).encode()
    requete = urllib.request.Request("https://oauth2.googleapis.com/token", data=donnees)
    with urllib.request.urlopen(requete, timeout=60) as reponse:
        recu = json.load(reponse)
    _jeton["valeur"] = recu["access_token"]
    _jeton["expire"] = time.time() + int(recu.get("expires_in", 3600))
    return _jeton["valeur"]


def encoder(valeur):
    """Une valeur Python vers la représentation Firestore.

    L'ordre des tests n'est pas indifférent : un booléen est un entier en
    Python, et le tester après int en ferait un nombre.
    """
    if isinstance(valeur, bool):
        return {"booleanValue": valeur}
    if isinstance(valeur, int):
        return {"integerValue": str(valeur)}
    if isinstance(valeur, float):
        return {"doubleValue": valeur}
    if isinstance(valeur, str):
        return {"stringValue": valeur}
    if valeur is None:
        return {"nullValue": None}
    if isinstance(valeur, (list, tuple)):
        return {"arrayValue": {"values": [encoder(v) for v in valeur]}}
    if isinstance(valeur, dict):
        return {"mapValue": {"fields": {k: encoder(v) for k, v in valeur.items()}}}
    raise ValueError(f"type non convertible : {type(valeur)}")


def pousser(etat, config=None):
    """Réécrit users/<uid>/courriel/etat et rend la taille envoyée.

    Le masque énumère les champs de premier niveau : c'est la seule forme de
    PATCH dont le comportement est écrit noir sur blanc dans l'API. Sans masque,
    le sort des champs absents du corps dépend de détails d'implémentation, et
    un champ fantôme d'une ancienne version resterait à traîner dans le document
    sans que personne sache d'où il vient.
    """
    config = config or charger_config()
    if not config.get("projet") or not config.get("uid"):
        raise RuntimeError(
            f"configuration incomplète : ni {CONFIG} ni {CONFIG_LANCEUR} ne donnent "
            "« projet » et « uid »")
    chemin = f"{base_url(config)}/users/{config['uid']}/{COLLECTION}/{DOCUMENT}"
    masque = "&".join(f"updateMask.fieldPaths={champ}" for champ in etat)
    corps = json.dumps({"fields": {k: encoder(v) for k, v in etat.items()}}).encode()
    requete = urllib.request.Request(f"{chemin}?{masque}", data=corps, method="PATCH")
    requete.add_header("Authorization", f"Bearer {jeton_acces(config)}")
    requete.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(requete, timeout=120) as reponse:
            reponse.read()
    except urllib.error.HTTPError as erreur:
        detail = erreur.read().decode("utf-8", "replace")[:600]
        raise RuntimeError(f"Firestore a refusé l'écriture ({erreur.code}) : {detail}") from None
    return len(corps)


def lire(config=None):
    """Relit le document poussé — sert à vérifier après un envoi."""
    config = config or charger_config()
    chemin = f"{base_url(config)}/users/{config['uid']}/{COLLECTION}/{DOCUMENT}"
    requete = urllib.request.Request(chemin)
    requete.add_header("Authorization", f"Bearer {jeton_acces(config)}")
    try:
        with urllib.request.urlopen(requete, timeout=60) as reponse:
            return json.loads(reponse.read() or b"{}")
    except urllib.error.HTTPError as erreur:
        if erreur.code == 404:
            return {}
        raise
