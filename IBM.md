# Hackathon Gen AI 2026 - Proposition IBM

## Sujet
**Système MultiAgents d'IA Générative pour l'Automatisation de la Livraison Logicielle**

## Contexte
Avec l'émergence de solutions agentiques, une opportunité d'optimisation des activités de développement est apparue.

Question directrice : comment accélérer les activités de développement au moyen d'outils et d'agents assistant un développeur dans ses tâches récurrentes ?

## Objectif
Les participants doivent concevoir et implémenter un système multiagents reposant sur des LLM open source et des frameworks agentiques.

Le système doit prendre en entrée une expression de besoin (spécifications fonctionnelles, croquis, supports visuels d'idéation, maquettes, etc.) et produire de manière autonome les principaux artefacts d'un pipeline de livraison logicielle.

## Capacités Attendues Du Système
- Analyser les spécifications d'entrée et identifier modules, parcours utilisateurs, contraintes et composants nécessaires.
- Créer automatiquement un environnement de développement :
	- Initialiser un dépôt git
	- Générer l'ossature du projet
	- Définir et configurer une pipeline CI/CD (GitHub Actions, GitLab CI, Azure DevOps, etc.)
- Produire une documentation d'architecture basée sur le modèle C4 (https://c4model.com/) :
	- Diagramme de contexte
	- Diagramme de conteneurs
	- Diagramme de composants
	- Documentation technique descriptive
- Générer le code de l'application :
	- Composants et services principaux
	- Logique métier
	- Suites de tests unitaires
	- Commentaires explicatifs
	- Structure de projet maintenable
- Générer une interface adaptable au contexte client, permettant l'intégration :
	- De la charte graphique
	- De bibliothèques de composants internes
	- D'éléments UI personnalisés
- Exposer les traces de raisonnement (Plan / Act / Reason).

## Contraintes Techniques
Les participants doivent :
- Utiliser au moins un LLM open source.
- Utiliser un framework agentique open source (LangChain, LangGraph, Haystack, Semantic Kernel, AutoGen, LlamaIndex Agents, etc.).

Le choix doit être objectivé : quels critères auront été significatifs ?

## Validation
Le jury évalue la solution selon les phases et critères suivants.

### Phase 1 : Compréhension de l'entrée
- Les agents interprètent les spécifications fournies et identifient les éléments fonctionnels clés.
- Le système produit un plan initial ou une trace de raisonnement.

### Phase 2 : Mise en place de l'environnement
- Un dépôt git fonctionnel est généré.
- Une pipeline CI/CD est automatiquement créée.
- La mise en place est reproductible.

### Phase 3 : Production de la documentation
- Les diagrammes C4 sont générés et cohérents avec les besoins fonctionnels.
- La documentation technique est claire et complète.

### Phase 4 : Génération du code
- Le code compile et s'exécute.
- Les tests unitaires s'exécutent correctement.
- L'architecture reflète la documentation produite.
- Le code est lisible et commenté.

### Phase 5 : Adaptabilité
- L'interface ou l'application peut être reconfigurée avec les éléments graphiques du client.
- L'intégration d'une librairie interne nécessite peu d'intervention manuelle.

### Phase 6 : Autonomie et comportement des agents
- Le système montre des étapes de raisonnement structurées.
- La collaboration entre agents est visible et efficace.
- L'utilisation de LLM open source est respectée.

### Critères finaux
- Faisabilité technique
- Complétude des livrables générés
- Innovation dans la conception des agents
- Qualité de la démonstration
- Potentiel de réutilisation en contexte entreprise

## Matériel À Disposition
### Description d'application candidate (vue d'ensemble)
Application proposée : une application simple de gestion de tâches (ToDo ou suivi de demandes) permettant d'illustrer des parcours utilisateurs, une logique métier et une interface graphique personnalisable. Elle est suffisamment riche pour tester le raisonnement multiagents tout en restant réaliste pour un hackathon.

### Périmètre fonctionnel
- Identification ou connexion utilisateur (simplifiée)
- Tableau de bord listant les tâches, filtrable
- Création, modification, suppression de tâches
- Page de détail avec description, priorité, date d'échéance, statut
- Optionnel : assignation à un utilisateur, commentaires, historique

## Résultats Attendus
- Livrer un prototype fonctionnel et une démonstration.

## Objectifs Avancés (Optionnels)
- Collaboration multiagents avec rôles distincts (Architecte, DevOps, Développeur, QA).
- Boucles d'autoévaluation ou d'autocorrection.
- Mise à disposition du générateur sous forme d'API.

