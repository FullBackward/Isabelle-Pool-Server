"""Training script for the sliding puzzle agent using TD learning."""

import csv
import os
import random
from pathlib import Path
from typing import List, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from tqdm import tqdm

from sliding_puzzle.agent import Agent
from sliding_puzzle.puzzle_env import IntBoard, PuzzleEnv, generate_scrambled_board
from sliding_puzzle.td_learning import td_playout

parent_dir = Path(__file__).parent
results_dir = parent_dir / "results"
results_dir.mkdir(parents=True, exist_ok=True)
checkpoints_dir = parent_dir / "checkpoints"
checkpoints_dir.mkdir(parents=True, exist_ok=True)


def create_evaluation_set(
    size: int = 3, max_scramble_depth: int = 10
) -> List[IntBoard]:
    """
    Create an evaluation set with scramble depths from 1 to num_problems.
    Each depth has exactly one problem.
    """
    eval_set = []
    for depth in range(1, max_scramble_depth + 1):
        # Set a fixed seed for reproducibility at each depth
        random.seed(42 + depth)
        board = generate_scrambled_board(size, depth)
        eval_set.append(board)
    return eval_set


def evaluate_agent(
    env: PuzzleEnv, agent: Agent, eval_set: List[IntBoard], max_moves: int
) -> float:
    """
    Evaluate the agent on the evaluation set.
    Returns the percentage of problems solved successfully.
    """
    agent.epsilon = 0.0  # No exploration during evaluation
    solved = 0

    for board in tqdm(eval_set, desc="Evaluating", leave=False):
        # Begin a new proof for this board
        env.begin_proof_for_start_state(board)
        moves_made = 0
        last_move = None

        # Continue making moves until solved or max_moves reached
        while not env.goal_board_reached() and moves_made < max_moves:
            move = agent.select_move(env, env.get_current_board(), last_move)
            env.make_move(move)
            last_move = move
            moves_made += 1

        if env.goal_board_reached():
            solved += 1

    # Reset epsilon for training
    agent.epsilon = 0.1

    return solved / len(eval_set) * 100.0


def train_agent(
    epochs: int = 100,
    puzzle_size: int = 3,
    initial_max_depth: int = 2,
    max_depth_increment: int = 1,
    depth_increment_epochs: int = 5,
    batch_size: int = 32,
    learning_rate: float = 0.01,
):
    """
    Train the agent using TD learning with curriculum learning.

    Args:
        epochs: Number of training epochs
        puzzle_size: Size of the puzzle (e.g., 3 for 3x3)
        initial_max_depth: Starting maximum scramble depth
        max_depth_increment: How much to increase the max depth
        depth_increment_epochs: How many epochs before increasing the depth
        batch_size: Number of playouts per batch
        learning_rate: Learning rate for the optimizer
    """
    # Initialize environment and agent
    env = PuzzleEnv(puzzle_size=puzzle_size)
    agent = Agent(size=puzzle_size)

    # Setup optimizer
    optimizer = optim.Adam(agent.value_network.parameters(), lr=learning_rate)

    # Create evaluation set
    eval_set = create_evaluation_set(puzzle_size, 10)

    # Initialize curriculum learning variables
    current_max_depth = initial_max_depth

    # Setup evaluation results file
    eval_results_path = os.path.join(results_dir, "evaluation_results.csv")
    with open(eval_results_path, "w", newline="") as csvfile:
        fieldnames = ["Epoch", "Max_Depth", "Success_Rate"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

    # Training loop
    for epoch in tqdm(range(1, epochs + 1), desc="Training", unit="epoch"):
        # Increase max depth if needed (curriculum learning)
        if epoch % depth_increment_epochs == 0 and current_max_depth < 20:
            current_max_depth += max_depth_increment
            current_max_depth = min(current_max_depth, 20)  # Cap at 20

        # Set maximum number of playouts for TD learning
        max_playout_moves = 2 * current_max_depth

        # Training batch - optimize after each example instead of accumulating loss
        for _ in tqdm(range(batch_size), desc="training batch", leave=False):
            # Generate a random board with current maximum depth
            depth = random.randint(1, current_max_depth)
            start_board = generate_scrambled_board(puzzle_size, depth)

            # Reset gradients for this example
            optimizer.zero_grad()

            # Perform TD learning playout
            loss = td_playout(env, agent, start_board, max_playout_moves)

            # Backpropagate and optimize immediately if there's any loss
            if loss > 0:
                loss_tensor = torch.tensor(loss, requires_grad=True)
                loss_tensor.backward()
                optimizer.step()

        # Evaluate agent after each epoch
        success_rate = evaluate_agent(env, agent, eval_set, 20)

        # Print results
        print(
            f"Epoch {epoch}, Max Depth: {current_max_depth}, Success Rate: {success_rate:.2f}%"
        )

        # Save evaluation results to CSV
        with open(eval_results_path, "a", newline="") as csvfile:
            writer = csv.DictWriter(
                csvfile, fieldnames=["Epoch", "Max_Depth", "Success_Rate"]
            )
            writer.writerow(
                {
                    "Epoch": epoch,
                    "Max_Depth": current_max_depth,
                    "Success_Rate": f"{success_rate:.2f}",
                }
            )

        # Save model checkpoint
        if epoch % 10 == 0:
            torch.save(
                agent.value_network.state_dict(),
                checkpoints_dir / "agent_epoch_{epoch}.pt",
            )

    # Save final model
    torch.save(
        agent.value_network.state_dict(),
        checkpoints_dir / "agent_final.pt",
    )

    return agent


if __name__ == "__main__":
    # Create checkpoints directory if it doesn't exist
    os.makedirs("/home/mt904/IsabelleGym/sliding_puzzle/checkpoints", exist_ok=True)

    # Train the agent
    trained_agent = train_agent(
        epochs=50,
        puzzle_size=3,
        initial_max_depth=1,
        max_depth_increment=1,
        depth_increment_epochs=3,
        batch_size=32,
        learning_rate=0.01,
    )
