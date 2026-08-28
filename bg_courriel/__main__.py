"""Ligne de commande de BGCourriel.

    python3 -m bg_courriel dossiers      ce que l'outil voit, sans rien compter
    python3 -m bg_courriel collecter     lit et trie, affiche, n'envoie rien
    python3 -m bg_courriel pousser       lit, trie et envoie vers Firestore
    python3 -m bg_courriel diagnostic    tout ce qui sert à comprendre une panne
    python3 -m bg_courriel regles        montre le classement, expéditeur par expéditeur
    python3 -m bg_courriel selftest      vérifie le lecteur et le tri

« collecter » N'ÉCRIT RIEN et c'est la commande à lancer en cas de doute :
regarder d'abord, envoyer ensuite.
"""

import argparse
import json
import os
import sys
import time

from . import __version__, analyse, classement, etat as mod_etat, firestore, thunderbird

DOSSIER_OUTIL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _date(horodatage):
    if not horodatage:
        return "jamais"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(horodatage))


def _poids_envoye(etat):
    """La taille RÉELLEMENT envoyée, pas celle du JSON simple.

    L'écart n'est pas anecdotique : la représentation Firestore enveloppe chaque
    valeur (« {"integerValue": "3"} » pour un 3) et pèse près du double. Afficher
    le JSON simple donnerait 155 Ko là où 267 Ko partent sur le réseau, et c'est
    le second chiffre qui approche la limite du mégaoctet.
    """
    return len(json.dumps({"fields": {k: firestore.encoder(v)
                                      for k, v in etat.items()}}).encode())


def _age(horodatage, maintenant=None):
    if not horodatage:
        return "—"
    secondes = max(0, (maintenant or time.time()) - horodatage)
    if secondes < 90:
        return f"il y a {int(secondes)} s"
    if secondes < 5400:
        return f"il y a {int(secondes / 60)} min"
    if secondes < 172800:
        return f"il y a {secondes / 3600:.1f} h"
    return f"il y a {secondes / 86400:.1f} j"


# ── commandes ───────────────────────────────────────────────────────────────

def cmd_dossiers(args):
    inventaire = thunderbird.inventaire(args.profil)
    if not inventaire["profil"]:
        print("Aucun profil Thunderbird trouvé.")
        print("Cherché dans :", ", ".join(thunderbird.RACINES))
        return 1
    print(f"Profil : {inventaire['profil']}")
    for compte in inventaire["comptes"]:
        etiquette = compte["adresse"] or compte["nom"]
        print(f"\n  {etiquette}  [{compte['genre'] or 'local'}]"
              f"{'  ' + compte['serveur'] if compte['serveur'] else ''}")
        if not compte["dossiers"]:
            print("      (aucun index .msf — dossier jamais ouvert dans Thunderbird)")
        for dossier in compte["dossiers"]:
            print(f"      {dossier['chemin_affiche']:<28} index écrit {_age(dossier['fraicheur'])}")
    ignores = ", ".join(sorted(thunderbird.DOSSIERS_IGNORES))
    print(f"\nDossiers volontairement écartés : {ignores}.")
    return 0


def _afficher_etat(etat):
    totaux = etat["totaux"]
    print(f"Relève du {_date(etat['releveLe'] / 1000)} sur {etat['machine']}")
    print(f"{totaux['boites']} boîte(s), {totaux['dossiers']} dossier(s), "
          f"{totaux['total']} courriels, {totaux['nonLus']} non lus, "
          f"{totaux['action']} en attente d'un geste")

    print("\n── Boîtes ─────────────────────────────────────────────────────────")
    for compte in etat["comptes"]:
        print(f"  {compte['adresse']:<34} {compte['total']:>5} courriels  "
              f"{compte['nonLus']:>4} non lus  {compte['action']:>4} à traiter  "
              f"index {_age(compte['fraicheur'])}")
        if compte["plusVieuxNonLu"]:
            print(f"      plus vieux non lu : {compte['plusVieuxNonLu']:.0f} jour(s)")

    print("\n── Catégories ─────────────────────────────────────────────────────")
    for nom in classement.CATEGORIES:
        part = totaux["categories"].get(nom)
        if not part:
            continue
        print(f"  {nom:<12} {part['total']:>5} courriels  {part['nonLus']:>4} non lus  "
              f"{part['action']:>4} à traiter")

    print("\n── File d'action (20 premiers) ────────────────────────────────────")
    if not etat["file"]:
        print("  Rien n'attend de geste.")
    for courriel in etat["file"][:20]:
        print(f"  [{courriel['action']:<12}] {courriel['ageJours']:>5.1f} j  "
              f"{courriel['categorie']:<10} {courriel['expediteurNom'][:26]:<26} "
              f"{courriel['objet'][:52]}")
    if etat["coupes"]["file"]:
        print(f"  … et {etat['coupes']['file']} de plus, non envoyés (borne de taille).")

    if etat["anomalies"]:
        print("\n── Anomalies ──────────────────────────────────────────────────────")
        for anomalie in etat["anomalies"]:
            print(f"  {anomalie['genre']:<16} {anomalie['texte']}")

    print("\n── Contrôle contre les compteurs de Thunderbird ───────────────────")
    for controle in etat["controle"]["parDossier"]:
        marque = "ok " if controle["ok"] else "ÉCART"
        print(f"  {marque} {controle['compte']:<30} {controle['dossier']:<12} "
              f"compté {controle['total']:>4}/{controle['nonLus']:>4} non lus, "
              f"annoncé {controle['totalAnnonce']:>4}/{controle['nonLusAnnonce']:>4}")

    if etat["plaintes"]:
        print("\n── Plaintes ───────────────────────────────────────────────────────")
        for plainte in etat["plaintes"]:
            print(f"  {plainte}")


def cmd_collecter(args):
    etat, _ = mod_etat.construire(DOSSIER_OUTIL, args.profil, args.seuil,
                                  journaliser=False)
    if args.json:
        print(json.dumps(etat, ensure_ascii=False, indent=2))
        return 0
    _afficher_etat(etat)
    print(f"\nRien n'a été envoyé (« collecter » n'écrit pas). "
          f"Le document pèserait {_poids_envoye(etat) / 1024:.0f} Ko.")
    return 0


def cmd_pousser(args):
    config = firestore.charger_config()
    etat, _ = mod_etat.construire(DOSSIER_OUTIL, args.profil, args.seuil)
    if not args.silencieux:
        _afficher_etat(etat)
    poids = firestore.pousser(etat, config)
    cible = (f"users/{config['uid']}/{firestore.COLLECTION}/{firestore.DOCUMENT}")
    print(f"\nEnvoyé : {poids / 1024:.0f} Ko vers {cible} "
          f"(projet {config['projet']}).")
    return 0


def cmd_diagnostic(args):
    print(f"BGCourriel {__version__}")
    print(f"Python {sys.version.split()[0]} — dossier {DOSSIER_OUTIL}")

    print("\n── Thunderbird ────────────────────────────────────────────────────")
    profil = args.profil or thunderbird.trouver_profil()
    print(f"  profil : {profil or 'AUCUN'}")
    if profil:
        verrou = os.path.join(profil, "lock")
        ouvert = os.path.islink(verrou) or os.path.exists(verrou)
        print(f"  Thunderbird semble {'OUVERT' if ouvert else 'fermé'} "
              f"({'verrou présent' if ouvert else 'aucun verrou'})")
        if ouvert:
            print("  → un index peut retarder de quelques minutes sur l'écran ;")
            print("    la colonne « index écrit » de « dossiers » donne l'heure réelle.")

    print("\n── Règles de classement ───────────────────────────────────────────")
    regles = classement.charger_regles(DOSSIER_OUTIL)
    fichier = os.path.join(DOSSIER_OUTIL, classement.FICHIER_REGLES)
    print(f"  fichier : {fichier if os.path.exists(fichier) else 'absent (règles de départ)'}")
    print(f"  {len(regles.regles)} règle(s) chargée(s)")
    for plainte in regles.plaintes:
        print(f"  PLAINTE : {plainte}")

    print("\n── Firestore ──────────────────────────────────────────────────────")
    config = firestore.charger_config()
    print(f"  configuration : {config['source'] or 'AUCUNE'}")
    print(f"  projet : {config['projet'] or '—'}")
    print(f"  uid    : {config['uid'] or '—'}")
    print(f"  clé    : {config['cle_sa']} "
          f"({'présente' if os.path.exists(config['cle_sa']) else 'ABSENTE'})")
    print(f"  cible  : users/<uid>/{firestore.COLLECTION}/{firestore.DOCUMENT}")
    if args.reseau and config.get("projet"):
        try:
            document = firestore.lire(config)
            if document:
                champs = document.get("fields", {})
                releve = champs.get("releveLe", {}).get("integerValue")
                print(f"  document en ligne : {len(champs)} champs, "
                      f"dernière relève {_date(int(releve) / 1000) if releve else '?'}")
            else:
                print("  document en ligne : absent (jamais poussé)")
        except (RuntimeError, OSError) as erreur:
            print(f"  LECTURE IMPOSSIBLE : {erreur}")
    else:
        print("  (ajouter --reseau pour interroger Firestore)")

    print("\n── Journal des tendances ──────────────────────────────────────────")
    journal = analyse.lire_historique(DOSSIER_OUTIL)
    print(f"  {analyse.chemin_historique(DOSSIER_OUTIL)}")
    print(f"  {len(journal)} relève(s) gardée(s)"
          + (f", de {_date(journal[0]['t'])} à {_date(journal[-1]['t'])}" if journal else ""))

    print("\n── Collecte d'essai ───────────────────────────────────────────────")
    debut = time.time()
    etat, tous = mod_etat.construire(DOSSIER_OUTIL, args.profil, args.seuil,
                                     journaliser=False)
    duree = time.time() - debut
    print(f"  {len(tous)} courriels lus en {duree:.2f} s")
    print(f"  document : {_poids_envoye(etat) / 1024:.0f} Ko sur les 1024 Ko permis")
    print(f"  contrôle : {'concorde avec Thunderbird' if etat['controle']['toutOk'] else 'ÉCART — voir ci-dessous'}")
    for controle in etat["controle"]["parDossier"]:
        if not controle["ok"]:
            print(f"    {controle['compte']} / {controle['dossier']} : "
                  f"compté {controle['total']}/{controle['nonLus']}, "
                  f"annoncé {controle['totalAnnonce']}/{controle['nonLusAnnonce']}")
    for plainte in etat["plaintes"]:
        print(f"  PLAINTE : {plainte}")
    return 0


def cmd_regles(args):
    if args.ecrire:
        chemin = os.path.join(DOSSIER_OUTIL, classement.FICHIER_REGLES)
        if os.path.exists(chemin) and not args.forcer:
            print(f"{chemin} existe déjà — ajouter --forcer pour le remplacer.")
            return 1
        with open(chemin, "w", encoding="utf-8") as f:
            json.dump({"regles": classement.REGLES_PAR_DEFAUT}, f,
                      ensure_ascii=False, indent=2)
        print(f"Règles de départ écrites dans {chemin}.")
        print("Les modifier n'exige aucun redémarrage : la relève suivante les lit.")
        return 0

    tous, comptes, _, plaintes = mod_etat.collecter(DOSSIER_OUTIL, args.profil, args.seuil)
    for plainte in plaintes:
        print(f"PLAINTE : {plainte}")
    par_categorie = {}
    for courriel in tous:
        par_categorie.setdefault(courriel["categorie"], []).append(courriel)
    for categorie in classement.CATEGORIES:
        liste = par_categorie.get(categorie) or []
        if not liste:
            continue
        print(f"\n── {categorie} ({len(liste)} courriels) "
              + "─" * max(0, 50 - len(categorie)))
        expediteurs = {}
        for courriel in liste:
            entree = expediteurs.setdefault(courriel["expediteur"], {"n": 0, "regle": ""})
            entree["n"] += 1
            entree["regle"] = entree["regle"] or courriel.get("regle", "")
        for adresse, entree in sorted(expediteurs.items(), key=lambda x: -x[1]["n"])[:12]:
            print(f"  {entree['n']:>4}  {adresse:<48} {entree['regle']}")
    reste = par_categorie.get("autre") or []
    if reste:
        print(f"\n{len(reste)} courriels n'ont accroché aucune règle : ce sont les "
              f"candidats à une règle nouvelle.")
    return 0


def cmd_selftest(args):
    from . import selftest
    return selftest.executer(verbeux=args.verbeux)


# ── point d'entrée ──────────────────────────────────────────────────────────

def principal(argv=None):
    analyseur = argparse.ArgumentParser(
        prog="python3 -m bg_courriel",
        description="Tableau de bord des boîtes de courriel (Thunderbird, lecture seule).")
    analyseur.add_argument("--version", action="version", version=f"BGCourriel {__version__}")
    analyseur.add_argument("--profil", help="chemin d'un profil Thunderbird précis")
    analyseur.add_argument("--seuil", type=int, default=3, metavar="JOURS",
                           help="au-delà de combien de jours un courriel sans réponse "
                                "entre dans la file (défaut : 3)")
    sous = analyseur.add_subparsers(dest="commande")

    sous.add_parser("dossiers", help="ce que l'outil voit, sans rien compter")

    p = sous.add_parser("collecter", help="lit et trie, sans rien envoyer")
    p.add_argument("--json", action="store_true", help="sortir le document brut")

    p = sous.add_parser("pousser", help="lit, trie et envoie vers Firestore")
    p.add_argument("--silencieux", action="store_true",
                   help="n'afficher que le résultat de l'envoi (pour la minuterie)")

    p = sous.add_parser("diagnostic", help="tout ce qui sert à comprendre une panne")
    p.add_argument("--reseau", action="store_true", help="interroger aussi Firestore")

    p = sous.add_parser("regles", help="montre le classement obtenu, expéditeur par expéditeur")
    p.add_argument("--ecrire", action="store_true",
                   help=f"écrire {classement.FICHIER_REGLES} avec les règles de départ")
    p.add_argument("--forcer", action="store_true", help="remplacer un fichier existant")

    p = sous.add_parser("selftest", help="vérifie le lecteur Mork et le tri")
    p.add_argument("--verbeux", action="store_true", help="détailler chaque vérification")

    args = analyseur.parse_args(argv)
    if not args.commande:
        analyseur.print_help()
        return 0
    commandes = {
        "dossiers": cmd_dossiers, "collecter": cmd_collecter, "pousser": cmd_pousser,
        "diagnostic": cmd_diagnostic, "regles": cmd_regles, "selftest": cmd_selftest,
    }
    return commandes[args.commande](args)


if __name__ == "__main__":
    sys.exit(principal())
