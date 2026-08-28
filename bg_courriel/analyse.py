"""Volumétrie, tendances et repérage d'anomalies.

CE QUI SE MESURE SANS MÉMOIRE, ET CE QUI EN DEMANDE UNE. Le volume par jour,
les principaux expéditeurs, la part de bruit : tout cela se recalcule à chaque
relève à partir des dates portées par les courriels eux-mêmes. En revanche
« mon nombre de non lus grimpe depuis une semaine » ne se lit nulle part dans
une boîte : c'est une comparaison entre deux instants. Un journal local garde
donc une empreinte par relève — quelques dizaines d'octets — et c'est la seule
donnée que l'outil produit au lieu de la lire.

LES ANOMALIES SONT COMPARÉES À L'HABITUDE, JAMAIS À UN SEUIL FIXE. « Plus de 20
courriels par jour » ne veut rien dire : c'est beaucoup pour la boîte github@ et
peu pour un jour de campagne. Chaque mesure est donc rapportée à la médiane de
la boîte elle-même. La médiane, pas la moyenne : une seule journée à 80
courriels tirerait une moyenne vers le haut pendant des semaines et masquerait
justement les afflux suivants.

CE QUE CES SIGNAUX NE SONT PAS. Un silence repéré ici veut dire « aucun
courriel n'est arrivé alors qu'il en arrive habituellement ». Cela peut être une
panne de réception, mais aussi un congé ou un client parti. L'outil signale et
nomme ce qu'il a mesuré ; il ne conclut pas.
"""

import json
import os
import statistics
import time

FICHIER_HISTORIQUE = "etat/historique.json"
JOURS_HISTORIQUE = 400          # environ un an de relèves, à quelques Ko
JOURS_SERIE = 90                # profondeur des courbes affichées


def _jour(horodatage):
    """La date locale d'un horodatage, en AAAA-MM-JJ.

    Locale et non UTC : un courriel de 21 h le mardi appartient au mardi pour
    l'œil qui lit le tableau, même s'il est mercredi à Greenwich.
    """
    return time.strftime("%Y-%m-%d", time.localtime(horodatage))


def _mediane(valeurs):
    valeurs = [v for v in valeurs if v is not None]
    return statistics.median(valeurs) if valeurs else 0


# ── volumétrie ──────────────────────────────────────────────────────────────

def series_par_jour(courriels, comptes, jours=JOURS_SERIE, maintenant=None):
    """Nombre de courriels reçus par jour, en tout et par boîte.

    Les jours sans courriel sont présents avec un zéro : une courbe qui saute
    les jours vides dessine une activité continue là où il n'y a rien.
    """
    maintenant = maintenant if maintenant is not None else time.time()
    debut = maintenant - jours * 86400
    adresses = [c.get("adresse", "") for c in comptes if c.get("adresse")]
    jours_liste = [_jour(maintenant - n * 86400) for n in range(jours - 1, -1, -1)]
    vides = {j: 0 for j in jours_liste}
    total = dict(vides)
    par_compte = {a: dict(vides) for a in adresses}
    for courriel in courriels:
        recu = courriel.get("recu") or 0
        if recu < debut:
            continue
        jour = _jour(recu)
        if jour not in total:
            continue                       # courriel daté du futur : ignoré ici
        total[jour] += 1
        compte = courriel.get("compte", "")
        if compte in par_compte:
            par_compte[compte][jour] += 1
    return {
        "jours": jours_liste,
        "total": [total[j] for j in jours_liste],
        "par_compte": {a: [par_compte[a][j] for j in jours_liste] for a in adresses},
    }


def principaux_expediteurs(courriels, limite=15, jours=JOURS_SERIE, maintenant=None):
    """Les expéditeurs les plus présents, avec leur poids en non lus.

    Le classement se fait sur la fenêtre affichée et non sur toute la boîte :
    un expéditeur très bavard il y a deux ans n'est pas un sujet d'aujourd'hui.
    """
    maintenant = maintenant if maintenant is not None else time.time()
    debut = maintenant - jours * 86400
    par_adresse = {}
    for courriel in courriels:
        if (courriel.get("recu") or 0) < debut:
            continue
        adresse = courriel.get("expediteur") or "(inconnu)"
        entree = par_adresse.setdefault(adresse, {
            "expediteur": adresse,
            "nom": courriel.get("expediteur_nom") or adresse,
            "total": 0, "nonLus": 0, "dernier": 0,
            "categorie": courriel.get("categorie", "autre"),
        })
        entree["total"] += 1
        if not courriel.get("lu"):
            entree["nonLus"] += 1
        entree["dernier"] = max(entree["dernier"], courriel.get("recu") or 0)
    classe = sorted(par_adresse.values(), key=lambda e: (-e["total"], e["expediteur"]))
    return classe[:limite]


def parts_categories(courriels):
    """Combien de courriels par catégorie, et combien y sont non lus."""
    parts = {}
    for courriel in courriels:
        categorie = courriel.get("categorie", "autre")
        entree = parts.setdefault(categorie, {"total": 0, "nonLus": 0, "action": 0})
        entree["total"] += 1
        if not courriel.get("lu"):
            entree["nonLus"] += 1
        if courriel.get("action"):
            entree["action"] += 1
    return parts


# ── journal local ───────────────────────────────────────────────────────────

def chemin_historique(dossier):
    return os.path.join(dossier, FICHIER_HISTORIQUE)


def lire_historique(dossier):
    chemin = chemin_historique(dossier)
    if not os.path.exists(chemin):
        return []
    try:
        with open(chemin, encoding="utf-8") as f:
            donnees = json.load(f)
        return donnees if isinstance(donnees, list) else []
    except (OSError, ValueError):
        # Un journal corrompu ne vaut pas une relève perdue : on repart de zéro.
        return []


def ecrire_historique(dossier, entrees, maintenant=None):
    """Ajoute une empreinte et rend le journal complet.

    Écriture par fichier temporaire puis remplacement : une coupure de courant
    au milieu d'un écriture laisserait sinon un journal tronqué, et c'est
    exactement le fichier dont on ne veut pas relire les restes.
    """
    maintenant = maintenant if maintenant is not None else time.time()
    limite = maintenant - JOURS_HISTORIQUE * 86400
    journal = [e for e in lire_historique(dossier) if (e.get("t") or 0) >= limite]
    journal.append(entrees)
    chemin = chemin_historique(dossier)
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    provisoire = chemin + ".tmp"
    with open(provisoire, "w", encoding="utf-8") as f:
        json.dump(journal, f, ensure_ascii=False)
    os.replace(provisoire, chemin)
    return journal


def empreinte(courriels, comptes, maintenant=None):
    """Ce qu'on garde d'une relève : des compteurs, aucun contenu.

    Le journal sert à dessiner une évolution, pas à rejouer le passé. Y mettre
    des objets ou des adresses ferait grossir un fichier local sans rien ajouter
    à la courbe.
    """
    maintenant = maintenant if maintenant is not None else time.time()
    par_compte = {}
    for compte in comptes:
        adresse = compte.get("adresse", "")
        if not adresse:
            continue
        les_siens = [c for c in courriels if c.get("compte") == adresse]
        par_compte[adresse] = {
            "total": len(les_siens),
            "nonLus": sum(1 for c in les_siens if not c.get("lu")),
            "action": sum(1 for c in les_siens if c.get("action")),
        }
    return {"t": int(maintenant), "comptes": par_compte}


def evolution_non_lus(journal, jours=30):
    """La courbe du non-lu : une valeur par jour, la dernière relève du jour.

    La dernière et non la moyenne : le tableau répond à « où j'en étais à la fin
    de cette journée-là », qui est la question qu'on se pose en regardant si le
    retard se creuse.
    """
    par_jour = {}
    for entree in journal:
        horodatage = entree.get("t") or 0
        jour = _jour(horodatage)
        # Tolérant à l'ancien nom « non_lus » : un journal écrit avant
        # l'harmonisation des noms de champs doit rester lisible, sinon la
        # courbe repart de zéro sans que personne comprenne pourquoi.
        total = sum(v.get("nonLus", v.get("non_lus", 0))
                    for v in (entree.get("comptes") or {}).values())
        action = sum(v.get("action", 0) for v in (entree.get("comptes") or {}).values())
        precedent = par_jour.get(jour)
        if precedent is None or horodatage >= precedent["t"]:
            par_jour[jour] = {"t": horodatage, "nonLus": total, "action": action}
    derniers = sorted(par_jour.items())[-jours:]
    return {
        "jours": [j for j, _ in derniers],
        "nonLus": [v["nonLus"] for _, v in derniers],
        "action": [v["action"] for _, v in derniers],
    }


# ── anomalies ───────────────────────────────────────────────────────────────

def _volumes_quotidiens(courriels, jours, maintenant):
    debut = maintenant - jours * 86400
    par_jour = {}
    for courriel in courriels:
        recu = courriel.get("recu") or 0
        if recu >= debut:
            par_jour[_jour(recu)] = par_jour.get(_jour(recu), 0) + 1
    return par_jour


def anomalies(courriels, comptes, jours=60, maintenant=None):
    """Ce qui sort de l'ordinaire, chaque signal comparé à l'habitude de sa boîte.

    Trois signaux, et rien de plus : un tableau qui crie tout le temps ne se
    regarde plus.
    """
    maintenant = maintenant if maintenant is not None else time.time()
    trouvees = []

    for compte in comptes:
        adresse = compte.get("adresse", "")
        if not adresse:
            continue
        les_siens = [c for c in courriels if c.get("compte") == adresse]
        if not les_siens:
            continue
        volumes = _volumes_quotidiens(les_siens, jours, maintenant)
        # Les jours sans courriel comptent pour zéro : sans eux, la médiane
        # d'une boîte calme serait celle de ses seuls jours actifs, et le
        # moindre courriel passerait pour un afflux.
        serie = [volumes.get(_jour(maintenant - n * 86400), 0) for n in range(1, jours + 1)]
        habituel = _mediane(serie)
        aujourdhui = volumes.get(_jour(maintenant), 0)

        # 1. Afflux — au moins trois courriels, et le triple de l'habitude.
        if aujourdhui >= 3 and habituel and aujourdhui >= 3 * habituel:
            trouvees.append({
                "genre": "afflux", "compte": adresse,
                "texte": f"{aujourdhui} courriels aujourd'hui sur {adresse}, "
                         f"contre {habituel:.0f} par jour d'habitude",
                "valeur": aujourdhui, "reference": habituel,
            })

        # 2. Silence — rapporté à l'intervalle habituel entre deux courriels,
        #    pas à un nombre de jours choisi arbitrairement.
        dates = sorted((c.get("recu") or 0) for c in les_siens)
        recents = [d for d in dates if d >= maintenant - jours * 86400]
        if len(recents) >= 5:
            ecarts = [(b - a) / 86400.0 for a, b in zip(recents, recents[1:])]
            ecart_habituel = _mediane(ecarts)
            silence = (maintenant - recents[-1]) / 86400.0
            if ecart_habituel > 0 and silence >= max(2.0, 5 * ecart_habituel):
                trouvees.append({
                    "genre": "silence", "compte": adresse,
                    "texte": f"rien reçu sur {adresse} depuis {silence:.1f} jour(s) ; "
                             f"l'écart habituel est de {ecart_habituel:.2f} jour(s)",
                    "valeur": round(silence, 2), "reference": round(ecart_habituel, 2),
                })

    # 3. Expéditeur habituel qui cesse d'écrire. Au moins quatre courriels pour
    #    qu'on puisse parler d'habitude, et une régularité mesurable.
    par_expediteur = {}
    for courriel in courriels:
        if courriel.get("categorie") in ("infolettre",):
            continue
        par_expediteur.setdefault(courriel.get("expediteur", ""), []).append(
            courriel.get("recu") or 0)
    for adresse, dates in par_expediteur.items():
        if not adresse or len(dates) < 4:
            continue
        dates = sorted(dates)
        ecarts = [(b - a) / 86400.0 for a, b in zip(dates, dates[1:])]
        ecart_habituel = _mediane(ecarts)
        silence = (maintenant - dates[-1]) / 86400.0
        etendue = (dates[-1] - dates[0]) / 86400.0
        # Deux garde-fous ajoutés après la première mesure réelle, où l'outil
        # annonçait « écrivait tous les 0.0 jour(s) » : un expéditeur qui a
        # envoyé quatre courriels dans la même minute n'a aucune habitude à
        # trahir. Il faut un écart mesurable ET une histoire assez longue pour
        # qu'on puisse parler de régularité.
        if not 0.5 <= ecart_habituel <= 30 or etendue < 14:
            continue
        if silence >= max(14.0, 4 * ecart_habituel):
            trouvees.append({
                "genre": "expediteur_muet", "compte": "",
                "texte": f"{adresse} écrivait tous les {ecart_habituel:.1f} jour(s) "
                         f"et s'est arrêté depuis {silence:.0f} jour(s)",
                "valeur": round(silence), "reference": round(ecart_habituel, 1),
            })

    ordre = {"afflux": 0, "silence": 1, "expediteur_muet": 2}
    trouvees.sort(key=lambda a: (ordre.get(a["genre"], 9), -(a.get("valeur") or 0)))
    return trouvees
