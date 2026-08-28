"""Le tri : à quelle catégorie appartient un courriel, et lequel attend un geste.

DEUX QUESTIONS DIFFÉRENTES, DEUX MÉCANIQUES DIFFÉRENTES.

  La CATÉGORIE dit de quoi il s'agit — une demande du site, une facture, une
  alerte technique, une infolettre. Elle se décide sur l'expéditeur et l'objet,
  par des règles écrites dans regles.json, modifiables sans toucher au code.

  La FILE D'ACTION dit ce qui attend le propriétaire. Elle ne se décide pas par
  règles mais par l'état du courriel : non lu, destinataire direct, pas encore
  répondu, et depuis combien de temps. Un courriel peut être « lu » et rester
  dans la file s'il attend une réponse depuis dix jours.

CE QU'ON PEUT SAVOIR ET CE QU'ON NE PEUT PAS. Le dossier « Envoyés » n'est pas
synchronisé sur cette machine (Thunderbird est réglé pour déposer les envois
dans les dossiers locaux, et ils sont vides). Impossible, donc, de dire « j'ai
répondu » en cherchant une réponse. Mais ce n'est pas nécessaire : Thunderbird
pose un drapeau « répondu » SUR LE COURRIEL D'ORIGINE, et ce drapeau vient du
serveur IMAP — il est donc juste même quand la réponse est partie du téléphone
ou du webmail. La file d'action s'appuie sur lui, pas sur une reconstruction.

L'ORDRE DES RÈGLES COMPTE, ET IL EST DÉLIBÉRÉ. « newsletter@formspree.io » et
« submissions@formspree.io » partagent un domaine mais rien d'autre : le
premier est du bruit, le second est un client qui écrit depuis le site. Les
règles sont donc essayées dans l'ordre du fichier, la première qui accroche
gagne, et les règles précises sont placées avant les règles de domaine.
"""

import json
import os
import re
import time

# Catégories connues. La liste est fermée à dessein : une catégorie inventée par
# une règle mal écrite apparaîtrait dans le tableau sans couleur ni ordre, et
# personne ne verrait l'erreur. Une catégorie inconnue déclenche une plainte au
# chargement des règles.
CATEGORIES = [
    "demande",      # un client ou un prospect écrit — formulaire du site inclus
    "affaires",     # échange en cours avec un client, un fournisseur, un partenaire
    "facture",      # facture, reçu, paiement, abonnement
    "technique",    # alertes des outils : GitHub, GoatCounter, hébergement, Firebase
    "sauvegarde",   # notification d'un produit de sauvegarde — voir sauvegardes.py
    "emploi",       # postulations, recruteurs, suivis de candidature
    "infolettre",   # infolettres, promotions, annonces de produits
    "autre",        # rien n'a accroché
]

# ── ce qui entre dans la file d'action, et ce qui n'y entre pas ─────────────
#
# CETTE PARTIE A ÉTÉ RÉÉCRITE APRÈS LA PREMIÈRE MESURE RÉELLE, et il vaut la
# peine de dire pourquoi. La première version mettait dans la file tout courriel
# non lu, plus tout courriel dont on n'avait pas encore répondu : sur les vraies
# boîtes, 337 courriels sur 426 y sont entrés. Une file qui contient 79 % de la
# boîte ne trie rien — elle recopie la boîte en changeant son titre.
#
# Le partage retenu répond à une seule question : est-ce que QUELQU'UN ATTEND
# QUELQUE CHOSE DE MOI ?
#
#   un humain attend une réponse            -> jusqu'à réponse, lu ou non
#   de l'argent en jeu                      -> dans la file si non lu, à tout âge
#   une offre d'emploi                      -> dans la file si non lu et pas périmée
#   une alerte d'outil, un courriel à trier -> dans la file si non lu ET récent
#   une infolettre                          -> jamais
#
# Le non-lu ancien d'une alerte d'outil ne disparaît pas pour autant : il est
# compté à part, sous « retard ». Un avis GitHub de jeton expiré vieux de six
# mois n'est plus une action — c'est de l'archivage — mais le cacher
# complètement serait mentir sur l'état de la boîte.

CATEGORIES_SANS_GESTE = {"infolettre"}

# Un humain a écrit et attend : la file les garde, lus ou non, jusqu'à réponse.
CATEGORIES_REPONSE = {"demande", "affaires"}

# L'argent ne se périme pas : une facture non lue d'il y a six mois compte
# toujours, parce qu'elle peut être impayée.
CATEGORIES_SANS_PEREMPTION = {"facture"}

# Au-delà de combien de jours un non-lu cesse d'être une action.
#
# Les trois durées viennent de ce que devient le courriel avec le temps, pas
# d'une préférence : une alerte d'outil de deux semaines est déjà de l'histoire ;
# une offre d'emploi d'un mois est probablement comblée ; un courriel d'humain
# LU et laissé sans réponse depuis trois mois n'attend plus personne — s'il
# était resté NON LU, il resterait dans la file, parce qu'un courriel de
# personne jamais ouvert est justement ce qu'on ne veut pas perdre.
JOURS_PEREMPTION = 14
JOURS_PEREMPTION_EMPLOI = 30
JOURS_ABANDON = 90

FICHIER_REGLES = "regles.json"

# Règles de départ, déduites du contenu réel des deux boîtes. Elles sont écrites
# ici et non seulement dans le fichier : un fichier de règles perdu ou mal formé
# ne doit pas rendre l'outil aveugle, il doit le faire retomber sur ce jeu-ci.
REGLES_PAR_DEFAUT = [
    # — Ce qui vient du site. Ces adresses sont celles de Formspree, qui porte
    #   les formulaires du site (à adapter si vous utilisez un autre service) :
    #   un message qui en vient EST une demande de client, pas une notification.
    {"categorie": "demande", "note": "formulaire du site",
     "expediteurs": ["submissions@formspree.io", "noreply@formspree.io",
                     "notifications@formspree.io"]},
    # Motifs ANCRÉS au début de l'objet, ou accompagnés de leur contexte. La
    # première version acceptait « new submission » n'importe où, et
    # l'infolettre de Formspree « Automatically Respond to New Submissions With
    # AI » est devenue une demande de client.
    {"categorie": "demande", "note": "objet des formulaires du site",
     "objet": [r"^\s*(nouvelle demande|nouvelle soumission|new submission)",
               r"new submission (from|received)",
               r"formulaire (du site|de contact|rempli|web)",
               r"pr(é|e)-?diagnostic",
               r"demande de (soutien|service|devis|estimation|rappel)"]},

    # — Notifications de sauvegarde. PLACÉE HAUT, ET LA RAISON EST LA MÊME
    #   QUE POUR LA CI : la règle générale « alertes et sécurité » plus bas
    #   accroche sur \balert\b, et « Backup Exec Alert: Job Failure » y
    #   tomberait avant d'atteindre cette règle-ci. Repérage SUJET SEULEMENT
    #   (signatures de produit reprises de backup-monitor/parsers.py,
    #   _detect_product) : bon marché, l'analyse fine du corps ne se fait
    #   qu'ensuite, et seulement pour ce qui accroche ici — voir corps.py et
    #   sauvegardes.py pour le pourquoi de cette séparation.
    {"categorie": "sauvegarde", "note": "produit de sauvegarde reconnu",
     "objet": [r"(?i)macrium", r"(?i)\breflect\b", r"(?i)retrospect",
               r"(?i)\bproactive\b", r"(?i)cobian",
               r"(?i)acronis drive monitor", r"(?i)backup exec",
               r"(?i)sql server job system",
               r"(?i)vzdump", r"(?i)proxmox-backup-client",
               r"(?i)garbage collect datastore", r"(?i)pruning datastore",
               r"(?i)sync remote .*datastore",
               # Convention de sujet des scripts maison ([Success]/[Failed]/
               # [Warning]) : plus large que les signatures ci-dessus, donc
               # placée en dernier dans cette règle — un vrai script de
               # sauvegarde l'utilise, mais rien d'autre dans vos boîtes ne
               # devrait l'employer.
               r"(?i)\[(success|failed|warning|error)\]"]},

    # — Domaines qui n'envoient JAMAIS rien d'actionnable. Placés tout en haut,
    #   juste après les formulaires du site, et la mesure explique pourquoi :
    #   « abonnement » est un mot d'argent légitime, et 49 avis Coursera
    #   « Votre abonnement a été annulé » se rangeaient parmi les factures — la
    #   seule catégorie qui ne se périme jamais. Ils y restaient donc pour
    #   toujours. Un domaine dont on sait qu'il ne fait que du courrier de masse
    #   doit être écarté AVANT que ses mots-clés ne soient lus.
    {"categorie": "infolettre", "note": "domaines de courrier de masse",
     "domaines": ["coursera.org", "credly.com", "residentadvisor.net", "ra.co",
                  "substack.com", "mailchimp.com", "sendgrid.net", "medium.com",
                  "producthunt.com", "bing.com", "augureai.ca", "formspree.io"]},

    # — Intégration continue. RÈGLE ÉTROITE, PLACÉE HAUT, ET LES DEUX COMPTENT.
    #
    #   Un dépôt de code peut très bien s'appeler « Facturation » ou
    #   contenir ce mot. Son nom voyage dans l'objet de chaque échec de
    #   test — « [Compte/Facturation] Run failed: … » — et le motif
    #   « factur » des mots d'argent y accrochait : une quarantaine d'échecs
    #   de CI se rangeaient dans « Factures et argent », la seule catégorie
    #   qui ne se périme jamais.
    #
    #   La correction évidente — remonter tout le domaine github.com au-dessus
    #   des mots d'argent — a été essayée et REJETÉE : elle emportait
    #   microsoft.com avec elle, et « Your Office 365 subscription has expired »
    #   cessait d'être une facture pour devenir une alerte. Or c'est bien de
    #   l'argent, et c'est le genre de courriel qu'on ne veut pas voir se périmer.
    #
    #   Ce qui est visé ici n'est donc pas un expéditeur mais une FORME d'objet :
    #   le préfixe « [propriétaire/dépôt] » qu'ajoute la forge, et le vocabulaire
    #   des exécutions. Aucun fournisseur ne facture avec un objet de cette
    #   forme.
    {"categorie": "technique", "note": "intégration continue et déploiement",
     "objet": [r"^\s*\[[\w.-]+/[\w.-]+\]",
               r"\brun (failed|cancelled|canceled|succeeded|completed)\b",
               r"\b(build|deploy(ment)?|pipeline|workflow) (failed|succeeded|cancelled)\b",
               r"\b(é|e)chec (du|de la) (test|construction|d(é|e)ploiement)"]},

    # — Alertes de jeton et de clé. AUSSI placée avant « facture » : la première
    #   version mettait « expir » dans les mots d'argent, et les avis GitHub de
    #   jeton d'accès expirant devenaient des factures.
    {"categorie": "technique", "note": "jeton, clé ou certificat qui expire",
     "objet": [r"(token|jeton|cl(é|e) (d'acc(è|e)s|ssh|api)|certificat|certificate"
               r"|public key)\b.{0,40}(expir|about to expire|revoked|r(é|e)voqu)",
               r"(added to|removed from|added a) your account",
               r"personal access token", r"deploy key", r"ssh (authentication )?key"]},

    # — Argent. Les mots d'argent seulement : aucun « expir » nu ici, il est
    #   ambigu et sa place est dans la règle ci-dessus.
    {"categorie": "facture", "note": "mots d'argent dans l'objet",
     "objet": [r"factur", r"invoice", r"re(ç|c)u de paiement", r"receipt",
               r"paiement", r"payment", r"virement", r"e-?transfer",
               r"abonnement", r"subscription", r"renouvellement",
               r"solde", r"relev(é|e) de compte", r"mode de paiement",
               r"carte de cr(é|e)dit", r"pr(é|e)l(è|e)vement", r"remboursement",
               r"has expired", r"a expir(é|e)"]},
    {"categorie": "facture", "note": "fournisseurs et paiements",
     "domaines": ["telus.com", "videotron.com", "hydroquebec.com", "interac.ca",
                  "payments.interac.ca", "stripe.com", "intuit.com", "wave.com",
                  "godaddy.com", "paypal.com", "desjardins.com"]},

    # — Outils et infrastructure, par leur domaine. Volontairement APRÈS les mots
    #   d'argent : plusieurs de ces fournisseurs facturent aussi, et une facture
    #   d'hébergement doit rester une facture.
    {"categorie": "technique", "note": "outils de développement et hébergement",
     # formspree.io est absent de cette liste À DESSEIN : ce fournisseur porte
     # les formulaires du site (une demande de client), son infolettre et ses
     # avis de compte. Le ranger en bloc ici rangerait son infolettre avec les
     # alertes. Ses courriels sont triés un par un par les autres règles.
     "domaines": ["github.com", "goatcounter.com", "google.com", "microsoft.com",
                  "office365.com", "office.com", "messaging.microsoft.com",
                  "cloudflare.com", "netlify.com", "vercel.com",
                  "namecheap.com", "letsencrypt.org", "duckdns.org"]},
    {"categorie": "technique", "note": "alertes et sécurité",
     "objet": [r"\balerte?s?\b", r"\balert\b", r"security", r"s(é|e)curit(é|e)",
               r"two-factor", r"deux (é|e)tapes", r"authentif",
               r"connexion inhabituelle", r"unusual sign", r"suspicious",
               r"mot de passe", r"password", r"panne", r"downtime",
               r"v(é|e)rifi(er|cation) (votre|your)", r"verify your"]},

    # — Recherche d'emploi.
    {"categorie": "emploi", "note": "plateformes de recrutement",
     "domaines": ["smartrecruiters.com", "indeed.com", "indeedemail.com",
                  "linkedin.com", "workday.com", "myworkday.com", "jobillico.com",
                  "emploisti.com", "workablemail.com", "ccq.org",
                  "quebecemploi.gouv.qc.ca", "emploiquebec.gouv.qc.ca"]},
    {"categorie": "emploi", "note": "adresses d'offres d'emploi",
     "expediteurs_motifs": [r"^(jobs?|emplois?|carrieres?|careers?|recrut)"]},
    {"categorie": "emploi", "note": "objets de candidature",
     "objet": [r"candidature", r"application (received|for)", r"entrevue",
               r"interview", r"votre postulation", r"offre d'emploi",
               r"nous voulons discuter", r"poste de", r"nouvelle offre"]},

    # — Bruit. Après tout le reste : ce qui n'a accroché aucune règle utile et
    #   ressemble à un envoi de masse. (Les domaines connus sont déjà écartés
    #   plus haut ; ce qui suit attrape les inconnus.)
    {"categorie": "infolettre", "note": "adresses d'envoi de masse",
     "expediteurs_motifs": [r"^(newsletter|news|marketing|hello|team|updates?"
                            r"|digest|bulletin|promo|offres?|nouvelles?)@"]},
    {"categorie": "infolettre", "note": "marques du courrier de masse",
     "objet": [r"se d(é|e)sabonner", r"unsubscribe", r"webinar", r"webinaire",
               r"promotion", r"rabais", r"\b\d+ ?% ?(de rabais|off)\b",
               r"nouveaut(é|e)s", r"what's new", r"newsletter",
               r"bienvenue (sur|dans)", r"welcome to", r"d(é|e)couvrez"]},

    # — DERNIER RECOURS, ET LA RÈGLE LA PLUS UTILE DU LOT. Tout ce qui reste et
    #   qui vient d'une adresse d'ALLURE HUMAINE est traité comme un échange
    #   d'affaires. La première mesure laissait « dominic@exemple-affaires.ca »
    #   dans « autre », au milieu de 168 courriels de robots : la seule personne
    #   qui écrivait vraiment était rangée avec le bruit. Les adresses de robots
    #   sont écartées par leur partie locale, qui les trahit toujours.
    {"categorie": "affaires", "note": "adresse d'allure humaine",
     "expediteurs_non": [r"^(no-?reply|ne-?pas-?repondre|nepasrepondre|donotreply"
                         r"|do-?not-?reply|notifications?|notify|mailer|bounce"
                         r"|postmaster|automated|auto|robot|daemon|admin"
                         r"|support|billing|verification|code|alerte?s?"
                         r"|security|info|contact|service|abuse|help)"
                         r"([-.+@]|$)"]},
]


class Regles:
    """Les règles chargées, prêtes à classer.

    Les expressions régulières sont compilées une fois : le tri passe sur
    quelques centaines de courriels à chaque relève, et compiler dans la boucle
    coûterait plus que tout le reste du programme.
    """

    def __init__(self, regles=None, plaintes=None):
        self.plaintes = plaintes if plaintes is not None else []
        self.regles = []
        for rang, brute in enumerate(regles if regles is not None else REGLES_PAR_DEFAUT):
            categorie = brute.get("categorie", "")
            if categorie not in CATEGORIES:
                self.plaintes.append(
                    f"règle {rang + 1} ignorée : catégorie inconnue « {categorie} » "
                    f"(connues : {', '.join(CATEGORIES)})")
                continue
            self.regles.append({
                "categorie": categorie,
                "note": brute.get("note", ""),
                "expediteurs": {a.lower() for a in brute.get("expediteurs", [])},
                "domaines": {d.lower().lstrip("@") for d in brute.get("domaines", [])},
                "objet": self._compiler(brute.get("objet", []), rang, "objet"),
                "expediteurs_motifs": self._compiler(
                    brute.get("expediteurs_motifs", []), rang, "expediteurs_motifs"),
                # Motifs qui EMPÊCHENT la règle d'accrocher. Une règle qui n'a
                # que ceux-là accroche tout le reste : c'est ainsi qu'est écrite
                # la règle de dernier recours (adresse d'allure humaine).
                "expediteurs_non": self._compiler(
                    brute.get("expediteurs_non", []), rang, "expediteurs_non"),
            })

    def _compiler(self, motifs, rang, champ):
        compiles = []
        for motif in motifs:
            try:
                compiles.append(re.compile(motif, re.I))
            except re.error as erreur:
                self.plaintes.append(
                    f"règle {rang + 1}, {champ} : motif « {motif} » illisible ({erreur})")
        return compiles

    @staticmethod
    def _domaine_accroche(domaine, domaines):
        """Le domaine ou l'un de ses sous-domaines.

        La comparaison exacte ne suffisait pas, et la mesure l'a montré :
        Coursera écrit depuis m.learn.coursera.org, t.mail.coursera.org,
        t.learn.coursera.org, m.mail.coursera.org et t.send.coursera.org. Une
        liste exacte aurait demandé de courir après chaque sous-domaine inventé
        par l'expéditeur ; « coursera.org » les prend tous.
        """
        return any(domaine == d or domaine.endswith("." + d) for d in domaines)

    def _accroche(self, regle, expediteur, domaine, objet):
        if regle["expediteurs_non"]:
            if not expediteur:
                return False
            if any(m.search(expediteur) for m in regle["expediteurs_non"]):
                return False
            purement_negative = not (regle["expediteurs"] or regle["domaines"]
                                     or regle["objet"] or regle["expediteurs_motifs"])
            if purement_negative:
                return True
        if expediteur and expediteur in regle["expediteurs"]:
            return True
        if domaine and self._domaine_accroche(domaine, regle["domaines"]):
            return True
        if expediteur and any(m.search(expediteur) for m in regle["expediteurs_motifs"]):
            return True
        if objet and any(m.search(objet) for m in regle["objet"]):
            return True
        return False

    def classer(self, courriel):
        """Rend (catégorie, note de la règle qui a accroché)."""
        expediteur = courriel.get("expediteur", "")
        domaine = courriel.get("domaine", "")
        objet = courriel.get("objet", "") or ""
        for regle in self.regles:
            if self._accroche(regle, expediteur, domaine, objet):
                return regle["categorie"], regle["note"]
        return "autre", ""


def charger_regles(dossier=None):
    """Charge regles.json s'il existe, sinon les règles de départ.

    Un fichier illisible ne fait pas tomber la relève : il produit une plainte,
    visible dans le diagnostic et dans le document poussé, et l'outil continue
    avec les règles de départ. Une relève qui s'arrête pour un point-virgule
    manquant laisserait le tableau de bord figé sur des chiffres vieux d'un
    jour, sans que rien ne l'annonce.
    """
    plaintes = []
    chemin = os.path.join(dossier or os.getcwd(), FICHIER_REGLES)
    brutes = None
    if os.path.exists(chemin):
        try:
            with open(chemin, encoding="utf-8") as f:
                contenu = json.load(f)
            brutes = contenu.get("regles") if isinstance(contenu, dict) else contenu
            if not isinstance(brutes, list):
                plaintes.append(f"{FICHIER_REGLES} : « regles » doit être une liste")
                brutes = None
        except (OSError, ValueError) as erreur:
            plaintes.append(f"{FICHIER_REGLES} illisible ({erreur}) — règles de départ")
            brutes = None
    return Regles(brutes, plaintes)


# ── file d'action ───────────────────────────────────────────────────────────

def _mes_adresses(comptes):
    return {c.get("adresse", "").lower() for c in comptes if c.get("adresse")}


def raison_action(courriel, mes_adresses, seuil_jours, maintenant=None):
    """Pourquoi ce courriel attend un geste, ou None s'il n'attend rien.

    Quatre raisons possibles, dans leur ordre d'urgence :

        marqué        le propriétaire l'a désigné à la main — rien ne passe devant
        sans réponse  un humain attend depuis plus longtemps que le seuil
        à répondre    un humain attend, mais depuis peu
        à voir        non lu, et sa catégorie mérite encore un regard

    Rendre None ne veut pas dire « sans importance » : un non-lu ancien d'alerte
    d'outil sort de la file et va au compte du retard (voir compter_retard).
    """
    if courriel.get("ignore"):
        return None
    categorie = courriel.get("categorie")
    if categorie in CATEGORIES_SANS_GESTE:
        return None
    maintenant = maintenant if maintenant is not None else time.time()
    age_jours = max(0.0, (maintenant - (courriel.get("recu") or 0)) / 86400.0)
    repondu = courriel.get("repondu")

    if courriel.get("marque") and not repondu:
        return "marqué"
    if categorie in CATEGORIES_REPONSE and not repondu:
        # Lu ou non : ce qui compte est qu'aucune réponse n'est partie. Le
        # drapeau « répondu » vient du serveur IMAP, il est donc juste même si
        # la réponse a été écrite du téléphone.
        if courriel.get("lu") and age_jours > JOURS_ABANDON:
            return None
        return "sans réponse" if age_jours >= seuil_jours else "à répondre"
    if not courriel.get("lu"):
        if categorie in CATEGORIES_SANS_PEREMPTION:
            return "à voir"
        limite = JOURS_PEREMPTION_EMPLOI if categorie == "emploi" else JOURS_PEREMPTION
        if age_jours <= limite:
            return "à voir"
    return None


def compter_retard(courriels):
    """Les non-lus qui ne sont plus dans la file : le retard accumulé.

    Ce compte existe pour qu'aucun courriel ne disparaisse entre deux chiffres.
    La file dit ce qui demande un geste maintenant ; le retard dit combien de
    courriels ont passé l'âge d'en demander un sans avoir été ouverts.
    """
    return sum(1 for c in courriels
               if not c.get("lu") and not c.get("action")
               and c.get("categorie") not in CATEGORIES_SANS_GESTE)


def appliquer(courriels, comptes, regles, seuil_jours=3, maintenant=None):
    """Classe tous les courriels et marque ceux qui attendent un geste.

    Modifie les courriels sur place et les rend : ils traversent ensuite les
    tendances et les anomalies, qui se servent de la catégorie.
    """
    mes = _mes_adresses(comptes)
    maintenant = maintenant if maintenant is not None else time.time()
    for courriel in courriels:
        categorie, note = regles.classer(courriel)
        courriel["categorie"] = categorie
        courriel["regle"] = note
        courriel["direct"] = bool(mes & set(courriel.get("destinataires") or []))
        courriel["age_jours"] = round(
            max(0.0, (maintenant - (courriel.get("recu") or 0)) / 86400.0), 2)
        courriel["action"] = raison_action(courriel, mes, seuil_jours, maintenant)
    return courriels
