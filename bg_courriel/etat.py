"""Construction du document que la page web affiche.

UN SEUL DOCUMENT, RÉÉCRIT EN ENTIER À CHAQUE RELÈVE. C'est la forme retenue par
les trois autres outils du site (TimeCalculator, marketing, BGFoods) et elle a
une raison : la page n'a alors qu'un seul abonnement à tenir, elle reçoit un
état cohérent ou rien, et il n'existe aucun moment où la moitié des chiffres
serait fraîche et l'autre non.

LA TAILLE EST UNE CONTRAINTE DURE. Un document Firestore ne peut pas dépasser
1 Mio, et la limite n'est pas négociable : une écriture qui la franchit est
refusée, donc le tableau resterait figé sur l'état de la veille. Trois mesures,
dans cet ordre de préférence :

  1. la file d'action porte le détail complet, parce que c'est là qu'on décide
     quoi faire ;
  2. la liste des courriels récents est bornée et ses textes sont coupés ;
  3. tout ce qui est écarté est COMPTÉ ET DIT dans le document lui-même
     (« coupes »). Une troncature silencieuse se lit comme « il n'y a que ça »,
     et c'est le pire des mensonges pour un tableau de bord.

CE DOCUMENT CONTIENT DES OBJETS ET DES ADRESSES EN CLAIR. C'est le choix
assumé : un tableau de tri sans objet ne sert à rien. La protection est
ailleurs — les règles Firestore ne laissent lire users/<uid>/courriel qu'au
compte connecté du propriétaire, et l'écriture ne vient pas du navigateur mais
d'un compte de service sur cette machine.
"""

import socket
import time

from . import analyse, classement, courriels as mod_courriels, sauvegardes, thunderbird

VERSION = 1

# Bornes. Choisies pour tenir très largement sous le mégaoctet : la file et les
# courriels récents réunis pèsent quelques dizaines de kilo-octets sur les
# boîtes mesurées (426 courriels).
MAX_FILE = 250
MAX_RECENTS = 400
MAX_OBJET = 180
MAX_APERCU = 160
JOURS_RECENTS = 90


def _couper(texte, limite):
    texte = (texte or "").strip()
    return texte if len(texte) <= limite else texte[:limite - 1].rstrip() + "…"


def _courriel_web(courriel, avec_apercu):
    """La forme réduite envoyée à la page. Rien d'inutile n'y entre."""
    reduit = {
        "id": courriel["id"],
        "compte": courriel["compte"],
        "dossier": courriel["dossier"],
        "objet": _couper(courriel["objet"], MAX_OBJET) or "(sans objet)",
        "expediteur": courriel["expediteur"],
        "expediteurNom": _couper(courriel["expediteur_nom"], 80),
        "recu": int(courriel["recu"] or 0),
        "ageJours": courriel.get("age_jours", 0.0),
        "categorie": courriel.get("categorie", "autre"),
        "lu": bool(courriel.get("lu")),
        "repondu": bool(courriel.get("repondu")),
        "marque": bool(courriel.get("marque")),
        "direct": bool(courriel.get("direct")),
        "taille": int(courriel.get("taille") or 0),
    }
    if courriel.get("action"):
        reduit["action"] = courriel["action"]
    if courriel.get("regle"):
        reduit["regle"] = courriel["regle"]
    if courriel.get("statutSauvegarde"):
        reduit["statutSauvegarde"] = courriel["statutSauvegarde"]
        if courriel.get("machineSauvegarde"):
            reduit["machineSauvegarde"] = courriel["machineSauvegarde"]
        if courriel.get("problemeSauvegarde"):
            reduit["problemeSauvegarde"] = _couper(
                courriel["problemeSauvegarde"], 160)
        if courriel.get("actionConseilleeSauvegarde"):
            reduit["actionConseilleeSauvegarde"] = _couper(
                courriel["actionConseilleeSauvegarde"], 220)
    if avec_apercu and courriel.get("apercu"):
        reduit["apercu"] = _couper(courriel["apercu"], MAX_APERCU)
    return reduit


def collecter(dossier_outil, profil=None, seuil_jours=3, maintenant=None):
    """Lit toutes les boîtes et rend (courriels, comptes, dossiers, plaintes).

    Rien n'est poussé ici : la collecte est séparée de l'envoi pour qu'on puisse
    la lancer et la regarder sans rien écrire nulle part (commande « collecter »).
    """
    maintenant = maintenant if maintenant is not None else time.time()
    plaintes = []
    profil = profil or thunderbird.trouver_profil()
    if not profil:
        return [], [], [], ["aucun profil Thunderbird trouvé"]

    prefs = thunderbird.lire_prefs(profil)
    comptes = thunderbird.comptes(profil, prefs)
    regles = classement.charger_regles(dossier_outil)
    plaintes.extend(regles.plaintes)

    # ── lecture de tous les dossiers ────────────────────────────────────────
    lus = []
    # Chemin du mbox de chaque dossier (le .msf, moins son suffixe) : ne sert
    # qu'à sauvegardes.enrichir(), pour lire À LA DEMANDE le corps complet
    # des seuls courriels repérés « sauvegarde » — voir corps.py.
    chemins_mbox = {}
    for compte in comptes:
        adresse = compte.get("adresse") or compte.get("nom") or compte["cle"]
        for dossier in thunderbird.dossiers(compte):
            try:
                base = thunderbird.lire_index(dossier["msf"])
            except (OSError, ValueError) as erreur:
                # Un dossier illisible ne fait pas tomber la relève : il est
                # nommé, et les autres boîtes sont quand même remontées.
                plaintes.append(f"{adresse} / {dossier['chemin_affiche']} illisible : {erreur}")
                continue
            liste = mod_courriels.lire_dossier(base, adresse, dossier["chemin_affiche"])
            chemins_mbox[(adresse, dossier["chemin_affiche"])] = dossier["msf"][:-4]
            lus.append({
                "compte": adresse,
                "dossier": dossier["chemin_affiche"],
                "cle": (adresse, dossier["cle"]),
                "fraicheur": dossier["fraicheur"],
                "courriels": liste,
                "annonce": mod_courriels.entete_dossier(base),
            })

    # ── un seul fichier d'index par dossier réel ────────────────────────────
    #
    # Thunderbird laisse parfois DEUX fichiers d'index pour le même dossier —
    # « Archive.msf » et « Archive-1.msf » — quand une resouscription recrée le
    # fichier au lieu de réutiliser l'existant. Les deux se lisent sans erreur,
    # et additionner leur contenu compterait le même courrier deux fois. On garde
    # celui que Thunderbird annonce comme le plus rempli, et on DIT lequel a été
    # écarté : un doublon silencieux est exactement le genre d'écart qu'on
    # passerait ensuite des heures à chercher.
    retenus = {}
    for entree in lus:
        garde = retenus.get(entree["cle"])
        if garde is None:
            retenus[entree["cle"]] = entree
            continue
        meilleur, autre = ((entree, garde)
                           if (entree["annonce"]["total_annonce"], entree["fraicheur"])
                           > (garde["annonce"]["total_annonce"], garde["fraicheur"])
                           else (garde, entree))
        retenus[entree["cle"]] = meilleur
        # On ne se plaint QUE si l'index écarté contenait quelque chose. Le cas
        # courant est deux fichiers vides — « Archive » et « Archive-1 », créés
        # par une resouscription — et rien n'est alors perdu. Une plainte à
        # chaque relève pour un doublon vide apprendrait surtout à ne plus lire
        # les plaintes.
        if autre["annonce"]["total_annonce"]:
            plaintes.append(
                f"{entree['compte']} : deux index pour le même dossier — "
                f"« {autre['dossier']} » ({autre['annonce']['total_annonce']} courriels) "
                f"écarté au profit de « {meilleur['dossier']} » "
                f"({meilleur['annonce']['total_annonce']} courriels)")

    tous = []
    infos_dossiers = []
    for entree in retenus.values():
        liste = entree["courriels"]
        tous.extend(liste)
        infos_dossiers.append({
            "compte": entree["compte"],
            "dossier": entree["dossier"],
            "total": len(liste),
            "nonLus": sum(1 for c in liste if not c.get("lu")),
            "totalAnnonce": entree["annonce"]["total_annonce"],
            "nonLusAnnonce": entree["annonce"]["non_lus_annonce"],
            "fraicheur": entree["fraicheur"],
        })
    infos_dossiers.sort(key=lambda d: (d["compte"], d["dossier"]))

    classement.appliquer(tous, comptes, regles, seuil_jours, maintenant)
    # Après le classement seulement : c'est lui qui décide quels courriels
    # sont « sauvegarde » (règle sur le sujet, bon marché). Le corps complet
    # n'est lu que pour ceux-là — voir sauvegardes.py et corps.py.
    sauvegardes.enrichir(tous, chemins_mbox, maintenant)
    return tous, comptes, infos_dossiers, plaintes


def construire(dossier_outil, profil=None, seuil_jours=3, maintenant=None,
               journaliser=True):
    """L'état complet, prêt à pousser."""
    maintenant = maintenant if maintenant is not None else time.time()
    tous, comptes, infos_dossiers, plaintes = collecter(
        dossier_outil, profil, seuil_jours, maintenant)

    # ── contrôle : notre comptage contre celui de Thunderbird ────────────────
    # Le meilleur autotest possible, parce qu'il ne dépend d'aucun chiffre écrit
    # en dur et reste vrai quand les boîtes changent. Un écart ne bloque pas la
    # relève : il est affiché, pour qu'on sache que le lecteur a dérivé.
    controles = []
    for infos in infos_dossiers:
        ok = (infos["total"] == infos["totalAnnonce"]
              and infos["nonLus"] == infos["nonLusAnnonce"])
        controles.append({**infos, "ok": ok})
        if not ok:
            plaintes.append(
                f"{infos['compte']} / {infos['dossier']} : compté "
                f"{infos['total']}/{infos['nonLus']} non lus, Thunderbird annonce "
                f"{infos['totalAnnonce']}/{infos['nonLusAnnonce']}")

    # ── comptes ─────────────────────────────────────────────────────────────
    resume_comptes = []
    for compte in comptes:
        adresse = compte.get("adresse") or compte.get("nom") or compte["cle"]
        les_siens = [c for c in tous if c["compte"] == adresse]
        siens_dossiers = [d for d in infos_dossiers if d["compte"] == adresse]
        resume_comptes.append({
            "adresse": adresse,
            "nom": compte.get("nom") or adresse,
            "nomAffiche": compte.get("nom_affiche", ""),
            "serveur": compte.get("serveur", ""),
            "genre": compte.get("genre", ""),
            "total": len(les_siens),
            "nonLus": sum(1 for c in les_siens if not c.get("lu")),
            "action": sum(1 for c in les_siens if c.get("action")),
            "dossiers": len(siens_dossiers),
            "fraicheur": max([d["fraicheur"] for d in siens_dossiers], default=0),
            "plusVieuxNonLu": max(
                [c["age_jours"] for c in les_siens if not c.get("lu")], default=0.0),
        })

    # ── file d'action, la partie qui compte ─────────────────────────────────
    file_complete = [c for c in tous if c.get("action")]
    # Le plus vieux d'abord : c'est l'ordre du risque, pas celui de l'arrivée.
    # Les courriels marqués passent devant, ils ont été désignés à la main.
    # Une sauvegarde en erreur suit tout de suite après : personne ne l'a
    # déclenchée à la main, mais rien n'attend plus qu'elle soit réparée.
    ordre_raison = {"marqué": 0, sauvegardes.ACTION_ERREUR: 1,
                    "sans réponse": 2, "à répondre": 3,
                    sauvegardes.ACTION_AVERTISSEMENT: 4, "à voir": 5}
    file_complete.sort(key=lambda c: (ordre_raison.get(c["action"], 9),
                                      -(c.get("age_jours") or 0)))
    file_web = [_courriel_web(c, True) for c in file_complete[:MAX_FILE]]

    # ── courriels récents ───────────────────────────────────────────────────
    limite_recents = maintenant - JOURS_RECENTS * 86400
    recents = [c for c in tous if (c.get("recu") or 0) >= limite_recents]
    recents.sort(key=lambda c: c.get("recu") or 0, reverse=True)
    recents_web = [_courriel_web(c, False) for c in recents[:MAX_RECENTS]]

    # ── tendances ───────────────────────────────────────────────────────────
    journal = []
    if journaliser:
        journal = analyse.ecrire_historique(
            dossier_outil, analyse.empreinte(tous, comptes, maintenant), maintenant)
    else:
        journal = analyse.lire_historique(dossier_outil)

    series = analyse.series_par_jour(tous, comptes, maintenant=maintenant)
    parts = analyse.parts_categories(tous)
    non_lus_total = sum(1 for c in tous if not c.get("lu"))

    etat = {
        "version": VERSION,
        "releveLe": int(maintenant * 1000),
        "machine": socket.gethostname(),
        "profil": thunderbird.trouver_profil() if profil is None else profil,
        "seuilJours": seuil_jours,
        "comptes": resume_comptes,
        "totaux": {
            "boites": len(resume_comptes),
            "dossiers": len(infos_dossiers),
            "total": len(tous),
            "nonLus": non_lus_total,
            "action": len(file_complete),
            # Les non-lus qui ne demandent plus de geste — alertes d'outils
            # périmées, courriels non triés trop vieux. Comptés ici pour que la
            # différence entre « non lus » et « à traiter » soit explicable.
            "retard": classement.compter_retard(tous),
            "categories": {k: v for k, v in sorted(parts.items())},
        },
        "file": file_web,
        "recents": recents_web,
        "tendances": {
            "jours": series["jours"],
            "total": series["total"],
            "parCompte": series["par_compte"],
            "topExpediteurs": analyse.principaux_expediteurs(tous, maintenant=maintenant),
            "evolution": analyse.evolution_non_lus(journal),
        },
        "anomalies": analyse.anomalies(tous, comptes, maintenant=maintenant),
        "controle": {
            "parDossier": controles,
            "toutOk": all(c["ok"] for c in controles) if controles else False,
        },
        "coupes": {
            "file": max(0, len(file_complete) - len(file_web)),
            "recents": max(0, len(recents) - len(recents_web)),
            "horsFenetre": len(tous) - len(recents),
        },
        "plaintes": plaintes,
    }
    return etat, tous
