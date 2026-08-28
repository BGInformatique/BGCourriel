"""Lecteur du format Mork — les fichiers .msf de Thunderbird.

POURQUOI CE FICHIER EXISTE. Thunderbird garde deux copies de chaque boîte : le
mbox (le texte des courriels) et le .msf (son index). On pourrait croire que
lire le mbox suffit — il contient tout. C'est faux pour l'état de lecture, et
la mesure est sans appel : sur une boîte de test, les courriels du mbox
s'annoncent TOUS lus (« X-Mozilla-Status: 0001 ») alors qu'une bonne partie
sont non lus. Sur un dossier IMAP, l'octet d'état du mbox est écrit au
téléchargement et Thunderbird ne le remet pas à jour : la vérité vit dans le
.msf. Un tableau de bord bâti sur le mbox afficherait « rien à traiter » sur
une boîte pleine de courriels non lus.

Le .msf a un deuxième avantage, dont on ne se prive pas : 178 Ko contre 30 Mo,
et il porte déjà l'objet, l'expéditeur, la date, la taille, le fil de
discussion et un aperçu du corps. On n'ouvre donc JAMAIS le mbox.

LECTURE SEULE, ET PAS SEULEMENT PAR POLITESSE. Le profil Thunderbird est une
base de données vivante : ce module ne fait qu'ouvrir des fichiers en « rb ».
Rien n'y est écrit, aucun verrou n'est pris. Voir thunderbird.lire_dossier()
pour la copie préalable qui évite de lire un fichier en cours d'écriture.

────────────────────────────────────────────────────────────────────────────
LE FORMAT, EN CE QU'IL A DE PIÉGEUX

Mork est un format texte de 1999 fait pour être écrit par ajout : le fichier
n'est jamais réécrit, chaque changement est collé à la fin. Lire l'état final
demande donc de rejouer le fichier du début à la fin, dans l'ordre.

  Dictionnaires   <(1F2=43f)(1C8=69a9f053)>
                  Une table d'atomes : identifiant hexadécimal = valeur. Les
                  valeurs longues (objets, adresses) n'apparaissent qu'ici,
                  une seule fois, et les lignes y renvoient.
                  Un dictionnaire précédé de <(a=c)> définit non pas des
                  valeurs mais des NOMS DE COLONNE (81=subject, 88=flags…) :
                  deux espaces de noms distincts qui partagent la même
                  numérotation hexadécimale. Les confondre donne du charabia.

  Lignes          [E2A:^80(^88=81)(^82^C9D)(^81^C9E)]
                  Identifiant, portée (^80 = les courriels), puis des cellules
                  (^colonne=valeur littérale) ou (^colonne^atome).

  Tables          {1:^80 {(k^96:c)(s=9)} E2A 12F:m }
                  Une liste d'appartenance. Le premier {…} est la méta-ligne :
                  « k » donne le genre de la table (^96 = la liste des
                  courriels du dossier). Les identifiants nus qui suivent sont
                  les membres.

  Transactions    @$${144{@ … @$$}144}@
                  Un groupe de changements. Sans effet sur la lecture : on
                  rejoue tout dans l'ordre, les marqueurs sont sautés.

TROIS PIÈGES QUI COÛTENT CHER

1. Le préfixe « - » sur une ligne ne supprime PAS la ligne : il la RÉÉCRIT
   ENTIÈREMENT — les cellules existantes sont oubliées, puis celles qui
   suivent sont posées. La preuve est dans le fichier : la ligne d'en-tête du
   dossier est réécrite en « [-1:^9F(^A1=131)(^A2=eb)…] », et si « - » voulait
   dire « supprimer », le nombre de courriels du dossier disparaîtrait à
   chaque relève. Lu comme une réécriture, il donne 0x131 = 305 courriels,
   ce que Thunderbird affiche. Le vrai retrait d'une ligne se fait par son
   retrait de la table (« -E26 » dans le corps d'une table).

2. Une valeur peut être coupée en plusieurs lignes physiques par une
   contre-oblique en fin de ligne, et le nom d'une colonne peut être séparé de
   son « = » par un saut de ligne et des espaces. Un analyseur qui travaille
   ligne par ligne, ou par expression régulière, se trompe sur les objets
   longs — ceux des vrais courriels.

3. Les accents ne sont pas dans le jeu de caractères annoncé en tête du
   fichier. Ils arrivent en octets échappés « $C3$A9 » (é) ou « $E2$80$99 »
   (l'apostrophe typographique) qu'il faut rassembler AVANT de décoder en
   UTF-8. Décoder trop tôt donne « Ã© ».
"""

# ── noms des colonnes qui nous intéressent ──────────────────────────────────
#
# Le .msf en compte une centaine ; celles-ci suffisent au tableau de bord.
# Les noms sont ceux de Thunderbird, pas les nôtres : on les traduit une seule
# fois, dans message() plus bas.

PORTEE_COURRIELS = "ns:msg:db:row:scope:msgs:all"
PORTEE_ENTETE = "ns:msg:db:row:scope:dbfolderinfo:all"
GENRE_COURRIELS = "ns:msg:db:table:kind:msgs"

# Drapeaux de nsMsgMessageFlags (mailnews/base/public/nsMsgMessageFlags.idl).
# Seuls ceux dont le tableau de bord se sert sont nommés.
LU = 0x000001
REPONDU = 0x000002
MARQUE = 0x000004
EXPURGE = 0x000008          # courriel effacé, la ligne survit dans le .msf
A_REPONDU_RE = 0x000010
HORS_LIGNE = 0x000080
SURVEILLE = 0x000100
TRANSFERE = 0x001000
NOUVEAU = 0x010000
IGNORE = 0x040000
EFFACE_IMAP = 0x200000      # effacé côté serveur, pas encore purgé

_HEXA = "0123456789abcdefABCDEF"


class Base:
    """L'état final du fichier, une fois toutes les transactions rejouées."""

    def __init__(self):
        self.atomes = {}     # id hexa -> valeur (portée des valeurs)
        self.colonnes = {}   # id hexa -> nom de colonne (portée des colonnes)
        self.lignes = {}     # (portée, id) -> {nom de colonne: valeur}
        self.tables = {}     # (portée, id) -> {"genre": str, "membres": [ids]}

    def lignes_de_portee(self, portee):
        return {rid: cel for (p, rid), cel in self.lignes.items() if p == portee}


class _Analyseur:
    """Automate à un seul passage sur le texte du fichier.

    Un automate plutôt que des expressions régulières parce que les valeurs
    peuvent contenir « ) », « ( » et des sauts de ligne échappés : aucune
    expression régulière ne délimite correctement une cellule.
    """

    def __init__(self, texte):
        self.t = texte
        self.i = 0
        self.n = len(texte)
        self.base = Base()

    # ── primitives ──────────────────────────────────────────────────────────

    def _sauter(self):
        """Espaces et commentaires « // » — jamais appelé dans une valeur."""
        while self.i < self.n:
            c = self.t[self.i]
            if c in " \t\r\n":
                self.i += 1
            elif c == "/" and self.t.startswith("//", self.i):
                fin = self.t.find("\n", self.i)
                self.i = self.n if fin < 0 else fin + 1
            else:
                return

    def _hexa(self):
        depart = self.i
        while self.i < self.n and self.t[self.i] in _HEXA:
            self.i += 1
        return self.t[depart:self.i]

    def _valeur(self):
        """Lit une valeur jusqu'à sa parenthèse fermante, celle-ci consommée.

        Rassemble les octets avant de décoder : voir le piège 3 de l'en-tête.
        """
        octets = bytearray()
        while self.i < self.n:
            c = self.t[self.i]
            if c == ")":
                self.i += 1
                break
            if c == "\\":
                self.i += 1
                if self.i >= self.n:
                    break
                suite = self.t[self.i]
                self.i += 1
                if suite == "\r":
                    # continuation en fin de ligne, à la mode Windows
                    if self.i < self.n and self.t[self.i] == "\n":
                        self.i += 1
                elif suite != "\n":
                    octets.extend(suite.encode("latin-1"))
                continue
            if c == "$" and self.i + 2 < self.n:
                paire = self.t[self.i + 1:self.i + 3]
                if paire[0] in _HEXA and paire[1] in _HEXA:
                    octets.append(int(paire, 16))
                    self.i += 3
                    continue
            octets.extend(c.encode("latin-1"))
            self.i += 1
        try:
            return octets.decode("utf-8")
        except UnicodeDecodeError:
            # Un .msf ancien peut porter du latin-1 brut. Mieux vaut un accent
            # approximatif qu'une exception au milieu d'une relève.
            return octets.decode("latin-1")

    def _cellule(self):
        """Lit « (colonne=valeur) », « (colonne^atome) » ou « (-colonne) ».

        Rend (nom de colonne, valeur, coupée). « coupée » vaut vrai pour
        « (-^88) » : la cellule doit disparaître de la ligne.
        """
        self.i += 1                     # (
        self._sauter()
        coupee = False
        if self.i < self.n and self.t[self.i] == "-":
            coupee = True
            self.i += 1
            self._sauter()
        if self.i < self.n and self.t[self.i] == "^":
            self.i += 1
            brut = self._hexa()
            nom = self.base.colonnes.get(brut, brut)
        else:
            depart = self.i
            while self.i < self.n and self.t[self.i] not in "=^)":
                self.i += 1
            nom = self.t[depart:self.i].strip()
        self._sauter()
        if self.i >= self.n:
            return nom, "", coupee
        c = self.t[self.i]
        if c == "=":
            self.i += 1
            return nom, self._valeur(), coupee
        if c == "^":
            self.i += 1
            atome = self._hexa()
            table = self.base.atomes
            if self.i < self.n and self.t[self.i] == ":":
                # « ^97:c » désigne un atome de la portée des colonnes
                self.i += 1
                if self.t[self.i:self.i + 1] == "c":
                    table = self.base.colonnes
                self.i += 1
            valeur = table.get(atome, "")
            self._sauter()
            if self.i < self.n and self.t[self.i] == ")":
                self.i += 1
            return nom, valeur, coupee
        # « (colonne) » sans valeur
        self.i += 1
        return nom, "", coupee

    def _portee(self, defaut=None):
        """Lit le « :portée » éventuel d'un identifiant de ligne ou de table."""
        if self.i >= self.n or self.t[self.i] != ":":
            return defaut
        self.i += 1
        if self.i < self.n and self.t[self.i] == "^":
            self.i += 1
            brut = self._hexa()
            return self.base.colonnes.get(brut, brut)
        depart = self.i
        while self.i < self.n and self.t[self.i] not in " \t\r\n([{}]":
            self.i += 1
        return self.t[depart:self.i]

    # ── constructions ───────────────────────────────────────────────────────

    def _dictionnaire(self):
        self.i += 1                     # <
        vers_colonnes = False
        while self.i < self.n:
            self._sauter()
            if self.i >= self.n:
                return
            c = self.t[self.i]
            if c == ">":
                self.i += 1
                return
            if c == "<":
                # méta-dictionnaire, en pratique le seul <(a=c)> du fichier :
                # il annonce que ce dictionnaire nomme des colonnes.
                self.i += 1
                while self.i < self.n and self.t[self.i] != ">":
                    self._sauter()
                    if self.i < self.n and self.t[self.i] == "(":
                        cle, valeur, _ = self._cellule()
                        if cle == "a" and valeur == "c":
                            vers_colonnes = True
                    elif self.i < self.n and self.t[self.i] != ">":
                        self.i += 1
                self.i += 1             # >
                continue
            if c == "(":
                cle, valeur, _ = self._cellule()
                cible = self.base.colonnes if vers_colonnes else self.base.atomes
                cible[cle] = valeur
                continue
            self.i += 1

    def _ligne(self, portee_heritee=None):
        self.i += 1                     # [
        self._sauter()
        reecriture = False
        if self.i < self.n and self.t[self.i] in "-+!":
            reecriture = self.t[self.i] == "-"
            self.i += 1
        rid = self._hexa()
        portee = self._portee(portee_heritee)
        cellules = []
        while self.i < self.n:
            self._sauter()
            if self.i >= self.n:
                break
            c = self.t[self.i]
            if c == "]":
                self.i += 1
                break
            if c == "(":
                cellules.append(self._cellule())
            else:
                self.i += 1
        cle = (portee, rid)
        if reecriture or cle not in self.base.lignes:
            # Piège 1 : « - » réécrit la ligne, il ne la supprime pas.
            self.base.lignes[cle] = {}
        ligne = self.base.lignes[cle]
        for nom, valeur, coupee in cellules:
            if coupee:
                ligne.pop(nom, None)
            else:
                ligne[nom] = valeur
        return rid, portee

    def _table(self):
        self.i += 1                     # {
        self._sauter()
        vidage = False
        if self.i < self.n and self.t[self.i] in "-+!":
            vidage = self.t[self.i] == "-"
            self.i += 1
        tid = self._hexa()
        portee = self._portee()
        cle = (portee, tid)
        table = self.base.tables.setdefault(cle, {"genre": "", "membres": []})
        if vidage:
            table["membres"] = []
        membres = table["membres"]
        meta_lue = False
        while self.i < self.n:
            self._sauter()
            if self.i >= self.n:
                break
            c = self.t[self.i]
            if c == "}":
                self.i += 1
                break
            if c == "{" and not meta_lue:
                # méta-ligne de la table : « k » donne son genre
                meta_lue = True
                self.i += 1
                while self.i < self.n and self.t[self.i] != "}":
                    self._sauter()
                    if self.i < self.n and self.t[self.i] == "(":
                        nom, valeur, _ = self._cellule()
                        if nom == "k":
                            table["genre"] = valeur
                    elif self.i < self.n and self.t[self.i] != "}":
                        # un identifiant nu peut suivre la méta-ligne
                        self._reference(membres, portee)
                self.i += 1             # }
                continue
            if c == "[":
                rid, _ = self._ligne(portee)
                if rid not in membres:
                    membres.append(rid)
                continue
            if c in _HEXA or c == "-":
                self._reference(membres, portee)
                continue
            self.i += 1

    def _reference(self, membres, portee):
        """Identifiant nu dans le corps d'une table : « E2A », « -E26 »."""
        retrait = False
        if self.t[self.i] == "-":
            retrait = True
            self.i += 1
        rid = self._hexa()
        if not rid:
            self.i += 1                 # caractère inattendu, on avance
            return
        self._portee(portee)
        if retrait:
            if rid in membres:
                membres.remove(rid)
        elif rid not in membres:
            membres.append(rid)

    # ── boucle principale ───────────────────────────────────────────────────

    def analyser(self):
        while self.i < self.n:
            self._sauter()
            if self.i >= self.n:
                break
            c = self.t[self.i]
            if c == "<":
                self._dictionnaire()
            elif c == "{":
                self._table()
            elif c == "[":
                self._ligne()
            elif c == "@":
                # marqueur de transaction @$${144{@ ou @$$}144}@ : on saute
                # jusqu'au « @ » qui le termine.
                fin = self.t.find("@", self.i + 3)
                self.i = self.n if fin < 0 else fin + 1
            else:
                self.i += 1
        return self.base


def analyser(texte):
    """Rejoue un fichier Mork et rend sa Base."""
    return _Analyseur(texte).analyser()


def analyser_fichier(chemin):
    with open(chemin, "rb") as f:
        # latin-1 : chaque octet devient un caractère, sans perte ni exception.
        # Le vrai décodage se fait valeur par valeur (piège 3).
        return analyser(f.read().decode("latin-1"))
