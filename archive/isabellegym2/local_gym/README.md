# IsabelleGym Agent System

A comprehensive agent interface system for Isabelle/HOL theorem proving with integrated support for Monte Carlo Tree Search (MCTS), reinforcement learning, and training data collection. The project is based on unpublished work by Milan Tom from the University of Cambridge, and updated by Zijing Li from the University of Edinburgh.

##

This is a working branch by Xuanwei Ren from the University of Edinburgh on improving previous version of IsabelleGym.

## Overview

This system provides a unified interface for developing and deploying theorem proving agents in the IsabelleGym environment. It features multi-session support, training data collection, proof visualization, and seamless integration with various proving strategies.

## Requirements

- Python 3.8+
- JVM 17+
- Isabelle/HOL system
- IsabelleGym environment
- Optional: Visualization dependencies for enhanced display

## File Structure

```
gym/
├── isabelle_agent_interface.py    # Main agent interface
├── mcts_agent.py                  # MCTS implementation
├── training_data_system.py        # Data collection system
├── proof_visualizer.py            # Visualization tools
├── isabelle_gym.py               # Core gym interface
└── success_checker.py            # Tactic success verification

```
The original IsabelleGym project which is the foundation of this project can be found from IsabelleGym.zip, also with the thesis.

## Local IsabelleGym Deployment
### 1. Use install.sh
chmod +x install.sh

./install.sh

### 2. Use Docker Compose

docker-compose up -d

pip install -r requirements.txt

If meeting "Bad component..." problem when compiling scala backend, try creating a empty main file under isabelle/Admin/components path.

## Detailed Information
### 1. Agent Interface (`gym/isabelle_agent_interface.py`)


The main interface defining the unified agent standard for Isabelle/HOL theorem proving.
#### Key Classes



**ProofContext**
- Represents the current state of an Isabelle proof
- Contains subgoals, priorities, proof depth, and resolution status
- Supports multi-objective priorities and Isabelle-specific features
- Key properties:
  - `active_goal_id`: Returns highest priority unsolved goal
  - `is_complete`: Checks if proof is complete
  - `num_subgoals`: Number of remaining subgoals
  - `progress_ratio`: Ratio of successful tactics

**ProofResult**
- Encapsulates the outcome and metrics of a proof attempt
- Contains success status, duration, proof steps, and final state
- Includes guidance calls and tactic recommendations for analysis

**IsabelleAgent (Abstract Base Class)**
- Defines the standard interface for all theorem proving agents
- Abstract methods:
  - `recommend_tactic()`: Generate next tactic for specified goal
  - `evaluate_priorities()`: Return priority scores for subgoals
- Concrete methods:
  - `prove_theorem()`: Main theorem proving method
  - `reset_session()`: Reset agent state between proofs


### 2. MCTS Agent (`gym/mcts_agent.py`)

Monte Carlo Tree Search interface implementation for theorem proving.


**MCTSProofContext**
- Extends `ProofContext` with MCTS-specific attributes
- Includes visit count, total value, children nodes, and parent references
- Supports tree search statistics and exhaustion tracking

**MCTSIsabelleAgent (Abstract Base Class)**
- Base class for MCTS theorem proving agents
- Abstract methods:
  - `estimate()`: Estimate state value
  - `select()`: Select search path from root to leaf
- Concrete methods:
  - `backup()`: Backpropagate value updates
  - `prove_theorem()`: Main MCTS search loop

**SimpleMCTSIsabelleAgent**
- Concrete implementation of MCTS agent
- Features:
  - Random value estimation for leaf nodes
  - UCB1 selection algorithm with exploration-exploitation balance
  - Content-based tactic selection strategy
  - Goal-specific tactic collections (primary, secondary, specialized)

### 3. Training Data System (`gym/training_data_system.py`)

Comprehensive system for collecting and managing training data from proof sessions.

#### Key Classes

**TacticInvocation**
- Records complete information for each tactic call
- Contains before/after states, tactic, success status, and execution metrics
- Includes used constants extraction and proof context
- Provides ML training format conversion

**ProofSession**
- Records complete proof session data
- Tracks theorem statement, agent name, duration, and success rate
- Contains all tactic invocations and session statistics
- Generates session summaries for analysis

**ConstantExtractor**
- Extracts mathematical constants and symbols from Isabelle tactics
- Identifies theorem references, mathematical constants, type constructors, and function names
- Filters Isabelle keywords and provides clean constant lists

**TrainingDataCollector**
- Main data collection interface
- Methods:
  - `start_session()`: Begin new proof session
  - `record_tactic_invocation()`: Record individual tactic calls
  - `end_session()`: Complete session and save data
  - `get_training_dataset()`: Retrieve filtered training data
  - `export_for_ml_training()`: Export data in ML formats (JSONL, CSV)
  - `generate_report()`: Generate collection statistics

**TrainingDataAgent**
- Wrapper agent that automatically collects training data
- Transparently wraps base agent methods
- Intercepts all tactic calls for data collection

#### Data Export Formats

- **JSONL**: Complete training records with input/target/metadata structure
- **CSV**: Simplified format for basic analysis
- **JSON**: Full session data with detailed statistics

### 4. Proof Visualizer (`gym/proof_visualizer.py`)

Interactive proof state visualization and session management.

#### Key Classes

**TacticResult**
- Enumeration for tactic execution results
- Values: `SUCCESS`, `FAILURE`, `PARTIAL`, `PENDING`

**TacticExecution**
- Records single tactic execution with timestamps
- Tracks before/after subgoals and execution metrics
- Provides progress detection and change analysis

**ProofSession**
- Manages complete proof session visualization
- Tracks executions, success rates, and duration
- Provides session summaries and statistics

**ProofVisualizer**
- Main visualization interface
- Features:
  - Session management with start/end tracking
  - Real-time tactic execution display
  - Current proof state visualization
  - Tactics history with progress indicators
  - Session summary generation
  - Data export to JSON format