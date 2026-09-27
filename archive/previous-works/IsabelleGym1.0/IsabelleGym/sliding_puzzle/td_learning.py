"""Temporal difference learning utilities"""

import random

import torch
import torch.nn as nn

from .agent import Agent
from .puzzle_env import IntBoard, Move, PuzzleEnv


def epsilon_greedy(
    agent: Agent, possible_next_boards: dict[Move, IntBoard]
) -> tuple[Move, float]:
    """
    Epsilon-greedy policy for selecting a move (random with probability epsilon or move
    that takes agent to board it believes has the least distance).
    """
    if random.random() < agent.epsilon:
        move, board = random.choice(list(possible_next_boards.items()))
        return move, agent.predict_distance(board).item()
    else:
        predicted_distances = {
            move: agent.predict_distance(board).item()
            for move, board in possible_next_boards.items()
        }
        return min(predicted_distances.items(), key=lambda move_dist: move_dist[1])


def td_playout(
    env: PuzzleEnv, agent: Agent, start_board: IntBoard, max_playout_moves: int
) -> float:
    """
    Perform a single playout according to the epsilon-greedy policy.
    Uses TD learning to update the agent's value network.
    Returns the total loss for the playout.
    """
    env.begin_proof_for_start_state(start_board)
    steps = 0
    values = []
    board = start_board

    # Store boards, moves, and values for TD updates
    trajectory = []

    while steps < max_playout_moves and not env.goal_board_reached():
        steps += 1
        possible_next_boards = env.get_possible_next_boards()
        next_move, value = epsilon_greedy(agent, possible_next_boards)

        # Store current state and predicted value
        trajectory.append((board, value))

        # Make the move
        env.make_move(next_move, board)
        board = possible_next_boards[next_move]

    # Terminal state
    if env.goal_board_reached():
        # Goal state has value 0
        trajectory.append((board, 0.0))
    elif trajectory:
        # Add the final state if we didn't reach the goal
        value = agent.predict_distance(board).item()
        trajectory.append((board, value))

    # Calculate TD loss
    total_loss = 0.0
    if len(trajectory) > 1:
        for i in range(len(trajectory) - 1):
            current_board, current_value = trajectory[i]
            next_board, next_value = trajectory[i + 1]

            # TD target: r + V(s') where r is -1 for each step
            target = next_value + 1.0  # +1 because the reward is -1 for each step

            # Loss is (target - prediction)^2
            loss = (target - current_value) ** 2
            total_loss += loss

    return total_loss
