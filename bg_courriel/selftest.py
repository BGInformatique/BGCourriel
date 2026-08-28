"""Autotest : le lecteur Mork, le tri, les tendances, l'encodage Firestore.

DEUX FAMILLES DE VÉRIFICATIONS, ET LA DEUXIÈME EST LA PLUS PRÉCIEUSE.

  Les vérifications sur données FABRIQUÉES tiennent le format : chaque piège du
  Mork a son cas, écrit à la main, minuscule et lisible. Elles tournent partout,
  y compris sur une machine sans Thunderbird.

  La vérification sur le PROFIL RÉEL ne compare pas à des chiffres écrits en
  dur — ils vieilliraient dès le prochain courriel. Elle compare notre comptage
  aux compteurs que Thunderbird tient LUI-MÊME dans ses index (numMsgs et
  numNewMsgs). Cet oracle se met à jour tout seul et reste valable quand les
  boîtes changent, quand on en ajoute une, quand on lit tout son courrier.

Aucune vérification n'écrit quoi que ce soit, ni sur disque ni dans Firestore.
"""

import json
import os
import time

from . import analyse, classement, courriels, etat as mod_etat, firestore, mork, thunderbird

DOSSIER_OUTIL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# En-tête minimal partagé par les cas fabriqués : les noms de colonnes et de
# portées dont le lecteur a besoin pour se repérer.
ENTETE = (
    "< <(a=c)> "
    "(80=ns:msg:db:row:scope:msgs:all)"
    "(81=subject)(82=sender)(86=date)(87=size)(88=flags)"
    "(8E=msgThreadId)(96=ns:msg:db:table:kind:msgs)"
    "(9F=ns:msg:db:row:scope:dbfolderinfo:all)(A1=numMsgs)(A2=numNewMsgs)>\n"
)


class _Bilan:
    def __init__(self, verbeux):
        self.verbeux = verbeux
        self.reussis = 0
        self.echecs = []

    def verifier(self, nom, obtenu, attendu):
        if obtenu == attendu:
            self.reussis += 1
            if self.verbeux:
                print(f"  ok    {nom}")
        else:
            self.echecs.append(f"{nom} : obtenu {obtenu!r}, attendu {attendu!r}")
            print(f"  ÉCHEC {nom} : obtenu {obtenu!r}, attendu {attendu!r}")

def _msgs(base):
    return base.lignes_de_portee(mork.PORTEE_COURRIELS)


# ── le format Mork ──────────────────────────────────────────────────────────

def tests_mork(bilan):
    base = mork.analyser(ENTETE + "<(A5=Bonjour)>\n[1:^80(^81^A5)(^88=81)]\n")
    ligne = _msgs(base).get("1", {})
    bilan.verifier("mork/atome renvoyé", ligne.get("subject"), "Bonjour")
    bilan.verifier("mork/valeur littérale", ligne.get("flags"), "81")

    # Deux espaces de noms distincts : l'atome 81 n'est pas la colonne 81.
    base = mork.analyser(ENTETE + "<(81=piège)>\n[1:^80(^81=vrai objet)]\n")
    bilan.verifier("mork/portées séparées", _msgs(base)["1"].get("subject"), "vrai objet")

    # Le piège central : « - » réécrit la ligne, il ne la supprime pas.
    base = mork.analyser(ENTETE + "[1:^80(^81=avant)(^88=80)]\n[-1:^80(^88=81)]\n")
    ligne = _msgs(base)["1"]
    bilan.verifier("mork/tiret réécrit la ligne", ligne.get("flags"), "81")
    bilan.verifier("mork/tiret oublie l'ancien", ligne.get("subject"), None)

    # Une cellule coupée disparaît, les autres restent.
    base = mork.analyser(ENTETE + "[1:^80(^81=objet)(^88=80)]\n[1:^80(-^81)]\n")
    ligne = _msgs(base)["1"]
    bilan.verifier("mork/cellule coupée", ligne.get("subject"), None)
    bilan.verifier("mork/cellule voisine gardée", ligne.get("flags"), "80")

    # Valeur coupée en deux lignes physiques, et nom séparé de son « = ».
    base = mork.analyser(ENTETE + "[1:^80(^81\n    =Deux\\\nmorceaux)]\n")
    bilan.verifier("mork/continuation", _msgs(base)["1"].get("subject"), "Deuxmorceaux")

    # Octets échappés : les accents arrivent par $XX et se décodent en UTF-8.
    base = mork.analyser(ENTETE + "[1:^80(^81=Re$C3$A7u l$E2$80$99avis)]\n")
    bilan.verifier("mork/octets échappés", _msgs(base)["1"].get("subject"), "Reçu l’avis")

    # Parenthèse et dollar littéraux.
    base = mork.analyser(ENTETE + "[1:^80(^81=Basic \\(no Teams\\) 5\\$)]\n")
    bilan.verifier("mork/échappement littéral", _msgs(base)["1"].get("subject"),
                   "Basic (no Teams) 5$")

    # Appartenance à une table : ajout puis retrait.
    base = mork.analyser(ENTETE + "{1:^80 {(k^96:c)(s=9)} 1 2 3}\n{1:^80 -2}\n")
    table = next(t for t in base.tables.values() if t["genre"] == mork.GENRE_COURRIELS)
    bilan.verifier("mork/membres de table", table["membres"], ["1", "3"])

    # Les marqueurs de transaction ne doivent pas perturber la lecture.
    base = mork.analyser(ENTETE + "@$${1{@\n[1:^80(^81=dans une transaction)]\n@$$}1}@\n")
    bilan.verifier("mork/transactions", _msgs(base)["1"].get("subject"),
                   "dans une transaction")

    # Les commentaires « // » sont du bruit, y compris avant l'en-tête.
    base = mork.analyser("// <!-- <mdb:mork:z v=\"1.4\"/> -->\n" + ENTETE
                         + "// un commentaire\n[1:^80(^81=après commentaire)]\n")
    bilan.verifier("mork/commentaires", _msgs(base)["1"].get("subject"),
                   "après commentaire")

    # L'en-tête de dossier, l'oracle interne de l'outil.
    base = mork.analyser(ENTETE + "[1:^9F(^A1=131)(^A2=ea)]\n")
    annonce = courriels.entete_dossier(base)
    bilan.verifier("mork/compteurs du dossier",
                   (annonce["total_annonce"], annonce["non_lus_annonce"]), (305, 234))

    # Un fichier vide ou tronqué ne doit pas lever d'exception.
    bilan.verifier("mork/fichier vide", len(mork.analyser("").lignes), 0)
    bilan.verifier("mork/fin abrupte",
                   len(mork.analyser(ENTETE + "[1:^80(^81=coupé").lignes), 1)


# ── courriels ───────────────────────────────────────────────────────────────

def tests_dossiers(bilan):
    """Les noms de dossiers : décodage IMAP et liste des écartés.

    Toute cette section vient d'un même incident. Au départ, chaque compte
    n'avait qu'un dossier téléchargé (INBOX) et une courte liste anglaise
    suffisait. Quand Thunderbird a synchronisé les 35 dossiers réels d'Office
    365, leurs noms sont arrivés en UTF-7 modifié, aucune comparaison n'a plus
    accroché, et 8 éléments supprimés, 16 indésirables et 3 brouillons sont
    entrés dans les totaux : 342 courriels au lieu de 305, sans qu'aucun
    courriel ne soit arrivé.
    """
    cas = [
        ("&AMk-l&AOk-ments envoy&AOk-s", "Éléments envoyés"),
        ("&AMk-l&AOk-ments supprim&AOk-s", "Éléments supprimés"),
        ("Courrier ind&AOk-sirable", "Courrier indésirable"),
        ("T&AOI-ches", "Tâches"),
        ("Bo&AO4-te d'envoi", "Boîte d'envoi"),
        ("Probl&AOg-mes de synchronisation", "Problèmes de synchronisation"),
        ("INBOX", "INBOX"),                       # rien à décoder
        ("Archive-1", "Archive-1"),               # le suffixe reste dans le nom
        ("R&-D", "R&D"),                          # « &- » est un & littéral
        ("&illisible-", "&illisible-"),           # séquence bancale : telle quelle
    ]
    for brut, attendu in cas:
        bilan.verifier(f"dossiers/décodage « {brut} »",
                       thunderbird.decoder_nom_imap(brut), attendu)

    # La normalisation retire accents, casse et suffixe de doublon — c'est elle
    # qui fait accrocher la liste des écartés.
    bilan.verifier("dossiers/normalisation complète",
                   thunderbird.normaliser_nom("&AMk-l&AOk-ments supprim&AOk-s-1"),
                   "elements supprimes")
    bilan.verifier("dossiers/suffixe de doublon retiré",
                   thunderbird.normaliser_nom("Brouillons-1"), "brouillons")

    # Chaque dossier réellement rencontré sur ce profil, et le sort attendu.
    ecartes = [
        "&AMk-l&AOk-ments envoy&AOk-s", "&AMk-l&AOk-ments envoy&AOk-s-1",
        "&AMk-l&AOk-ments supprim&AOk-s", "Courrier ind&AOk-sirable-1",
        "Brouillons-1", "Bo&AO4-te d'envoi", "Calendrier", "Contacts",
        "T&AOI-ches", "Notes", "Journal", "Flux RSS",
        "Historique des conversations", "Probl&AOg-mes de synchronisation",
        "Deleted Items", "Conversation History", "Sent", "Junk", "Trash",
    ]
    for nom in ecartes:
        bilan.verifier(f"dossiers/écarté « {thunderbird.decoder_nom_imap(nom)} »",
                       thunderbird.normaliser_nom(nom) in thunderbird.DOSSIERS_IGNORES,
                       True)

    # « Archive » est du courrier reçu, rangé : il doit être GARDÉ. C'est la
    # seule exception intéressante de la liste.
    for nom in ["INBOX", "Archive", "Archive-1", "Clients", "Sauvegardes"]:
        bilan.verifier(f"dossiers/gardé « {nom} »",
                       thunderbird.normaliser_nom(nom) in thunderbird.DOSSIERS_IGNORES,
                       False)


def tests_courriels(bilan):
    cellules = {
        "subject": "=?UTF-8?q?Nous_avons_re=C3=A7u_votre_commande.?=",
        "sender": "1|\"TELUS\" <TelusService@i.Telus.com>",
        "recipients": "<moi@exemple.ca>, autre@exemple.ca",
        "date": "69ab4cdc", "size": "dee5", "flags": "81",
        "preview": "Merci de votre commande",
    }
    courriel = courriels.convertir(cellules, "moi@exemple.ca", "INBOX")
    bilan.verifier("courriels/objet RFC 2047", courriel["objet"],
                   "Nous avons reçu votre commande.")
    bilan.verifier("courriels/préfixe du nom retiré", courriel["expediteur_nom"], "TELUS")
    bilan.verifier("courriels/adresse en minuscules", courriel["expediteur"],
                   "telusservice@i.telus.com")
    bilan.verifier("courriels/domaine", courriel["domaine"], "i.telus.com")
    bilan.verifier("courriels/destinataires", courriel["destinataires"],
                   ["moi@exemple.ca", "autre@exemple.ca"])
    bilan.verifier("courriels/taille hexadécimale", courriel["taille"], 0xdee5)
    bilan.verifier("courriels/drapeau lu", (courriel["lu"], courriel["repondu"]),
                   (True, False))

    drapeaux = courriels.convertir({"flags": str(hex(mork.REPONDU | mork.MARQUE))[2:]},
                                   "a", "b")
    bilan.verifier("courriels/répondu et marqué",
                   (drapeaux["repondu"], drapeaux["marque"], drapeaux["lu"]),
                   (True, True, False))

    efface = courriels.convertir({"flags": f"{mork.EXPURGE:x}"}, "a", "b")
    bilan.verifier("courriels/effacé écarté", courriels.vivant(efface), False)
    bilan.verifier("courriels/vivant gardé", courriels.vivant(courriel), True)

    bilan.verifier("courriels/sujet du fil",
                   courriels.sujet_normalise("Re: Tr: RE : Devis mars"), "devis mars")

    # Un objet déjà en clair ne doit pas être abîmé par le décodage RFC 2047.
    clair = courriels.convertir({"subject": "Facture 2026-08 — 129,99 $"}, "a", "b")
    bilan.verifier("courriels/objet en clair intact", clair["objet"],
                   "Facture 2026-08 — 129,99 $")


# ── classement ──────────────────────────────────────────────────────────────

def _courriel(**champs):
    base = {"expediteur": "", "domaine": "", "objet": "", "destinataires": [],
            "recu": time.time(), "lu": True, "repondu": False, "marque": False,
            "ignore": False, "categorie": "autre"}
    base.update(champs)
    if base["expediteur"] and not base["domaine"]:
        base["domaine"] = base["expediteur"].rsplit("@", 1)[-1]
    return base


def tests_classement(bilan):
    regles = classement.Regles()
    bilan.verifier("classement/règles chargées sans plainte", regles.plaintes, [])

    cas = [
        ("formulaire du site",
         _courriel(expediteur="submissions@formspree.io", objet="New submission"),
         "demande"),
        ("infolettre du même domaine",
         _courriel(expediteur="newsletter@formspree.io", objet="Form Prompts That Work"),
         "infolettre"),
        ("abonnement expiré : une facture",
         _courriel(expediteur="microsoft-noreply@microsoft.com",
                   objet="Your Office 365 E5 subscription has expired"),
         "facture"),
        # Le cas qui a fait réécrire les règles : « expire » dans les mots
        # d'argent rangeait 49 avis de jeton GitHub parmi les factures.
        ("jeton qui expire : technique, pas facture",
         _courriel(expediteur="notifications@github.com",
                   objet="[GitHub] Your personal access token (classic) is about to expire"),
         "technique"),
        ("alerte GitHub",
         _courriel(expediteur="noreply@github.com",
                   objet="A personal access token has been added"),
         "technique"),
        # Le dépôt s'appelle « BGFacturation » et son nom voyage dans l'objet de
        # chaque échec de test. Le motif « factur » y accrochait : une
        # quarantaine d'échecs de CI se rangeaient parmi les factures.
        ("échec de CI du dépôt BGFacturation : technique, pas facture",
         _courriel(expediteur="notifications@github.com",
                   objet="[BGInformatique/BGFacturation] Run failed: "
                         "Restrict authors — main (8c893c2)"),
         "technique"),
        ("sous-domaine d'infolettre",
         _courriel(expediteur="coursera@m.learn.coursera.org",
                   objet="Votre parcours continue"),
         "infolettre"),
        ("virement Interac",
         _courriel(expediteur="notify@payments.interac.ca",
                   objet="Un virement vous a été envoyé"),
         "facture"),
        ("offres d'emploi par l'adresse",
         _courriel(expediteur="jobs@emploisti.com", objet="Postes en TI"),
         "emploi"),
        ("adresse humaine : un échange d'affaires",
         _courriel(expediteur="dominic@exemple-affaires.ca", objet="Question sur le serveur"),
         "affaires"),
        ("robot malgré un domaine d'affaires",
         _courriel(expediteur="no-reply@exemple-affaires.ca", objet="Sauvegarde terminée"),
         "autre"),
        ("recruteur",
         _courriel(expediteur="notifications@smartrecruiters.com",
                   objet="Structube - Nous voulons discuter avec vous!"),
         "emploi"),
        ("infolettre par l'adresse",
         _courriel(expediteur="no-reply@t.learn.coursera.org", objet="New Course Added"),
         "infolettre"),
        # Un particulier qui écrit d'une adresse personnelle : la règle de
        # dernier recours en fait un échange d'affaires, ce qui le met dans la
        # file d'action. C'est exactement le courriel qu'il ne faut pas perdre.
        ("particulier inconnu : un échange d'affaires",
         _courriel(expediteur="client.inconnu@gmail.com", objet="Mon portable ne démarre plus"),
         "affaires"),
    ]
    for nom, courriel, attendu in cas:
        bilan.verifier(f"classement/{nom}", regles.classer(courriel)[0], attendu)

    # Une catégorie inventée est refusée, et le reste des règles continue de vivre.
    bancales = classement.Regles([
        {"categorie": "inventée", "domaines": ["exemple.ca"]},
        {"categorie": "facture", "domaines": ["exemple.ca"]},
    ])
    bilan.verifier("classement/catégorie inconnue signalée", len(bancales.plaintes), 1)
    bilan.verifier("classement/règles suivantes gardées",
                   bancales.classer(_courriel(expediteur="a@exemple.ca"))[0], "facture")

    # Un motif illisible est signalé sans faire tomber le chargement.
    cassee = classement.Regles([{"categorie": "facture", "objet": ["(non fermé"]}])
    bilan.verifier("classement/motif illisible signalé", len(cassee.plaintes), 1)


def tests_file_action(bilan):
    mes = {"moi@exemple.ca"}
    maintenant = 1_700_000_000.0
    jour = 86400

    def raison(**champs):
        return classement.raison_action(_courriel(**champs), mes, 3, maintenant)

    bilan.verifier("file/non lu récent à voir",
                   raison(lu=False, categorie="autre", recu=maintenant), "à voir")
    bilan.verifier("file/infolettre non lue reste dehors",
                   raison(lu=False, categorie="infolettre", recu=maintenant), None)
    bilan.verifier("file/marqué passe devant",
                   raison(lu=True, marque=True, categorie="autre", recu=maintenant),
                   "marqué")
    bilan.verifier("file/humain qui attend depuis longtemps",
                   raison(lu=True, categorie="affaires", recu=maintenant - 10 * jour,
                          destinataires=["moi@exemple.ca"]),
                   "sans réponse")
    bilan.verifier("file/répondu sort de la file",
                   raison(lu=True, repondu=True, categorie="affaires",
                          recu=maintenant - 10 * jour,
                          destinataires=["moi@exemple.ca"]), None)
    bilan.verifier("file/demande récente à répondre",
                   raison(lu=True, categorie="demande", recu=maintenant - jour,
                          destinataires=["moi@exemple.ca"]), "à répondre")
    bilan.verifier("file/fil ignoré reste dehors",
                   raison(lu=False, ignore=True, categorie="demande", recu=maintenant), None)

    # LE CAS QUI A FAIT RÉÉCRIRE LA FILE : une alerte d'outil non lue depuis six
    # mois n'est plus une action. Elle sort de la file et va au retard, sinon la
    # file recopie la boîte — 337 courriels sur 426 à la première mesure.
    bilan.verifier("file/alerte non lue périmée sort de la file",
                   raison(lu=False, categorie="technique", recu=maintenant - 180 * jour),
                   None)
    bilan.verifier("file/alerte non lue récente reste dedans",
                   raison(lu=False, categorie="technique", recu=maintenant - 2 * jour),
                   "à voir")
    bilan.verifier("file/facture non lue ne périme jamais",
                   raison(lu=False, categorie="facture", recu=maintenant - 180 * jour),
                   "à voir")
    bilan.verifier("file/offre d'emploi récente reste dedans",
                   raison(lu=False, categorie="emploi", recu=maintenant - 20 * jour),
                   "à voir")
    bilan.verifier("file/offre d'emploi de six mois sort de la file",
                   raison(lu=False, categorie="emploi", recu=maintenant - 180 * jour),
                   None)
    # Un humain vu et laissé sans réponse depuis six mois n'attend plus ;
    # le même courriel JAMAIS OUVERT reste dans la file, à tout âge.
    bilan.verifier("file/humain lu et abandonné sort de la file",
                   raison(lu=True, categorie="affaires", recu=maintenant - 180 * jour),
                   None)
    bilan.verifier("file/humain jamais ouvert reste, même vieux",
                   raison(lu=False, categorie="affaires", recu=maintenant - 180 * jour),
                   "sans réponse")

    # Le retard compte exactement ce que la file laisse tomber, et rien d'autre.
    lot = [
        _courriel(lu=False, categorie="technique", action=None),      # retard
        _courriel(lu=False, categorie="autre", action="à voir"),      # dans la file
        _courriel(lu=False, categorie="infolettre", action=None),     # ni l'un ni l'autre
        _courriel(lu=True, categorie="technique", action=None),       # rien
    ]
    bilan.verifier("file/retard compté à part", classement.compter_retard(lot), 1)


# ── tendances et anomalies ──────────────────────────────────────────────────

def tests_analyse(bilan):
    maintenant = 1_700_000_000.0
    jour = 86400
    comptes = [{"adresse": "a@exemple.ca"}, {"adresse": "b@exemple.ca"}]

    # Un courriel par jour pendant 5 jours sur a@, rien sur b@.
    liste = [_courriel(compte="a@exemple.ca", recu=maintenant - n * jour) for n in range(5)]
    for courriel in liste:
        courriel["compte"] = "a@exemple.ca"
    series = analyse.series_par_jour(liste, comptes, jours=10, maintenant=maintenant)
    bilan.verifier("analyse/une valeur par jour", len(series["jours"]), 10)
    bilan.verifier("analyse/jours vides à zéro", series["total"][:5], [0, 0, 0, 0, 0])
    bilan.verifier("analyse/jours pleins comptés", series["total"][5:], [1, 1, 1, 1, 1])
    bilan.verifier("analyse/boîte silencieuse présente",
                   sum(series["par_compte"]["b@exemple.ca"]), 0)

    # Afflux : une habitude d'un courriel par jour, puis dix d'un coup.
    habituel = [dict(_courriel(recu=maintenant - n * jour), compte="a@exemple.ca")
                for n in range(1, 40)]
    aujourdhui = [dict(_courriel(recu=maintenant - 60), compte="a@exemple.ca")
                  for _ in range(10)]
    trouvees = analyse.anomalies(habituel + aujourdhui, [comptes[0]], maintenant=maintenant)
    bilan.verifier("analyse/afflux repéré",
                   any(a["genre"] == "afflux" for a in trouvees), True)

    # Silence : une habitude de deux courriels par jour, puis vingt jours de rien.
    serres = []
    for n in range(20, 30):
        serres.append(dict(_courriel(recu=maintenant - n * jour), compte="a@exemple.ca"))
        serres.append(dict(_courriel(recu=maintenant - n * jour - 3600), compte="a@exemple.ca"))
    trouvees = analyse.anomalies(serres, [comptes[0]], maintenant=maintenant)
    bilan.verifier("analyse/silence repéré",
                   any(a["genre"] == "silence" for a in trouvees), True)

    # Une boîte normalement active ne déclenche rien.
    calme = [dict(_courriel(recu=maintenant - n * jour), compte="a@exemple.ca")
             for n in range(0, 30)]
    trouvees = analyse.anomalies(calme, [comptes[0]], maintenant=maintenant)
    bilan.verifier("analyse/boîte régulière silencieuse en anomalies", trouvees, [])

    # Expéditeur habituel qui se tait : il écrivait tous les 5 jours sur deux
    # mois, et plus rien depuis 60 jours.
    muet = [dict(_courriel(expediteur="client@exemple.ca", categorie="affaires",
                           recu=maintenant - (60 + 5 * n) * jour), compte="a@exemple.ca")
            for n in range(8)]
    trouvees = analyse.anomalies(muet, [comptes[0]], maintenant=maintenant)
    bilan.verifier("analyse/expéditeur muet repéré",
                   any(a["genre"] == "expediteur_muet" for a in trouvees), True)

    # Quatre courriels dans la même minute ne font pas une habitude : c'est le
    # cas qui produisait « écrivait tous les 0.0 jour(s) » à la première mesure.
    rafale = [dict(_courriel(expediteur="robot@exemple.ca", categorie="affaires",
                             recu=maintenant - 90 * jour - n), compte="a@exemple.ca")
              for n in range(6)]
    trouvees = analyse.anomalies(rafale, [comptes[0]], maintenant=maintenant)
    bilan.verifier("analyse/rafale n'est pas une habitude",
                   [a for a in trouvees if a["genre"] == "expediteur_muet"], [])

    # Évolution du non-lu : la dernière relève du jour fait foi.
    journal = [
        {"t": maintenant - jour, "comptes": {"a": {"nonLus": 10, "action": 2}}},
        {"t": maintenant - jour + 60, "comptes": {"a": {"nonLus": 12, "action": 3}}},
        {"t": maintenant, "comptes": {"a": {"nonLus": 5, "action": 1}}},
    ]
    evolution = analyse.evolution_non_lus(journal)
    bilan.verifier("analyse/deux jours dans la courbe", len(evolution["jours"]), 2)
    bilan.verifier("analyse/dernière relève du jour retenue",
                   evolution["nonLus"], [12, 5])

    # Un journal écrit avant l'harmonisation des noms de champs porte « non_lus ».
    # Il doit rester lisible, sinon la courbe repart de zéro sans explication.
    ancien = [{"t": maintenant, "comptes": {"a": {"non_lus": 7, "action": 2}}}]
    bilan.verifier("analyse/journal à l'ancien nom encore lu",
                   analyse.evolution_non_lus(ancien)["nonLus"], [7])


# ── encodage Firestore ──────────────────────────────────────────────────────

def tests_firestore(bilan):
    # Un booléen est un entier en Python : l'ordre des tests de encoder() compte.
    bilan.verifier("firestore/booléen avant entier", firestore.encoder(True),
                   {"booleanValue": True})
    bilan.verifier("firestore/entier en chaîne", firestore.encoder(3),
                   {"integerValue": "3"})
    bilan.verifier("firestore/liste de cartes",
                   firestore.encoder([{"a": 1}]),
                   {"arrayValue": {"values": [
                       {"mapValue": {"fields": {"a": {"integerValue": "1"}}}}]}})
    bilan.verifier("firestore/accents conservés", firestore.encoder("Reçu"),
                   {"stringValue": "Reçu"})
    try:
        firestore.encoder(object())
        bilan.verifier("firestore/type refusé", "aucune erreur", "ValueError")
    except ValueError:
        bilan.verifier("firestore/type refusé", "ValueError", "ValueError")


# ── profil réel ─────────────────────────────────────────────────────────────

def tests_profil_reel(bilan, verbeux):
    profil = thunderbird.trouver_profil()
    if not profil:
        print("  (aucun profil Thunderbird : vérifications sur données réelles sautées)")
        return
    print(f"  profil : {profil}")
    # journaliser=False : un autotest ne doit rien laisser derrière lui, pas
    # même une ligne dans le journal des tendances — elle fausserait la courbe.
    etat, tous = mod_etat.construire(DOSSIER_OUTIL, profil, journaliser=False)

    for controle in etat["controle"]["parDossier"]:
        nom = f"réel/{controle['compte']} {controle['dossier']}"
        bilan.verifier(f"{nom} total", controle["total"], controle["totalAnnonce"])
        bilan.verifier(f"{nom} non lus", controle["nonLus"], controle["nonLusAnnonce"])

    bilan.verifier("réel/au moins une boîte lue", etat["totaux"]["boites"] >= 1, True)
    bilan.verifier("réel/aucune plainte", etat["plaintes"], [])

    # La taille mesurée est celle de l'envoi Firestore, presque deux fois celle
    # du JSON simple : c'est elle qui doit rester sous la limite.
    poids = len(json.dumps({"fields": {k: firestore.encoder(v)
                                       for k, v in etat.items()}}).encode())
    bilan.verifier("réel/document sous le mégaoctet", poids < 1_000_000, True)
    if verbeux:
        print(f"  document : {poids / 1024:.0f} Ko, {len(tous)} courriels")

    # Les objets ne doivent pas rester encodés en MIME dans ce qui est envoyé.
    encodes = [c["objet"] for c in etat["file"] + etat["recents"] if "=?" in c["objet"]]
    bilan.verifier("réel/aucun objet MIME non décodé", encodes[:3], [])


# ── exécution ───────────────────────────────────────────────────────────────

def executer(verbeux=False):
    bilan = _Bilan(verbeux)
    sections = [
        ("format Mork", lambda: tests_mork(bilan)),
        ("noms de dossiers", lambda: tests_dossiers(bilan)),
        ("courriels", lambda: tests_courriels(bilan)),
        ("classement", lambda: tests_classement(bilan)),
        ("file d'action", lambda: tests_file_action(bilan)),
        ("tendances et anomalies", lambda: tests_analyse(bilan)),
        ("encodage Firestore", lambda: tests_firestore(bilan)),
        ("profil réel", lambda: tests_profil_reel(bilan, verbeux)),
    ]
    for nom, fonction in sections:
        print(f"── {nom} " + "─" * max(0, 60 - len(nom)))
        fonction()
    total = bilan.reussis + len(bilan.echecs)
    print()
    if bilan.echecs:
        print(f"{len(bilan.echecs)} ÉCHEC(S) sur {total} vérifications :")
        for echec in bilan.echecs:
            print(f"  {echec}")
        return 1
    print(f"{bilan.reussis}/{total} vérifications réussies.")
    return 0
