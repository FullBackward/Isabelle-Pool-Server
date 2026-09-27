import random
from typing import Optional

import torch
import torch.nn as nn

from sliding_puzzle.puzzle_env import IntBoard, Move, PuzzleEnv


class ValueNetwork(nn.Module):
    """Neural network to estimate the distance to the goal state."""

    def __init__(self, size, hidden_size=64):
        super(ValueNetwork, self).__init__()
        # One hot encoding of the size x size board
        self.input_size = size**2
        self.model = nn.Sequential(
            nn.Linear(self.input_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, 1),
            # Ensure non-negative output distances
            nn.ReLU(),
        )

    def board_to_input(self, board: IntBoard) -> torch.Tensor:
        """Convert the board to a one-hot encoded tensor."""
        return torch.tensor(board, dtype=torch.float32)

    def forward(self, x):
        """Computes a forward pass through the network."""
        return self.model(x)


class Agent:
    def __init__(self, size: int):
        self.value_network = ValueNetwork(size)
        self.epsilon = 0.1
        self.mcts = None  # Will be initialized when needed

    def predict_distance(self, board: IntBoard) -> float:
        """Predict the distance to the goal state using the value network."""
        return self.value_network(self.value_network.board_to_input(board))

    def select_move(
        self, env: PuzzleEnv, board: IntBoard, last_move: Optional[Move] = None
    ) -> Move:
        """Select a move using epsilon-greedy strategy with MCTS."""
        # Initialize MCTS if not already done
        if self.mcts is None:
            from sliding_puzzle.mcts import MCTS

            self.mcts = MCTS(env, self)

        # Epsilon-greedy: random exploration with probability epsilon
        if random.random() < self.epsilon:
            possible_moves = env.possible_moves(board, env.puzzle_size, last_move)
            return random.choice(possible_moves)

        # Use MCTS to select the best move
        return self.mcts.run(board, last_move)
