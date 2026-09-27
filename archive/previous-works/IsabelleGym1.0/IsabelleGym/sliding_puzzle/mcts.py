import math
from typing import Optional

import torch
import torch.nn.functional as F

from sliding_puzzle.agent import Agent
from sliding_puzzle.puzzle_env import IntBoard, Move, PuzzleEnv, do_move


class MCTSNode:
    """Node in the MCTS search tree."""

    def __init__(
        self,
        board: IntBoard,
        parent=None,
        move: Optional[Move] = None,
        prior: float = 0.0,
    ):
        self.board = board.copy()
        self.parent = parent
        self.move = move  # Move that led to this node
        self.children: dict[Move, MCTSNode] = {}

        # MCTS statistics
        self.visit_count = 0
        self.value_sum = 0.0
        self.prior = prior
        self.value = 0.0  # Value network evaluation

    def is_expanded(self) -> bool:
        """Check if the node has been expanded."""
        return len(self.children) > 0

    def get_mean_value(self) -> float:
        """Get the mean value of the node."""
        if self.visit_count == 0:
            return 0.0
        return self.value_sum / self.visit_count


class MCTS:
    """Monte Carlo Tree Search for the sliding puzzle."""

    def __init__(
        self,
        env: PuzzleEnv,
        agent: Agent,
        num_simulations: int = 2,
        c_puct: float = 1.0,
        epsilon: float = 1e-8,
    ):
        self.env = env
        self.agent = agent
        self.num_simulations = num_simulations
        self.c_puct = c_puct
        self.epsilon = epsilon
        self.size = env.puzzle_size
        self.root = None

    def initialize_search(self, board: IntBoard):
        """Initialize the search tree with the current board state."""
        self.root = MCTSNode(board)
        # Evaluate the root node
        self.root.value = self.agent.predict_distance(board).item()

    def select_leaf(
        self, node: MCTSNode, last_move: Optional[Move]
    ) -> tuple[MCTSNode, int, Optional[Move]]:
        """Selection phase: traverse the tree to a leaf node."""
        path_length = 0
        current = node
        current_last_move = last_move

        # Traverse the tree until reaching a leaf node
        while current.is_expanded() and not self.env.is_goal_board(current.board):
            move, current = self.select_child(current, current_last_move)
            current_last_move = move
            path_length += 1

        return current, path_length, current_last_move

    def select_child(
        self, node: MCTSNode, last_move: Optional[Move]
    ) -> tuple[Move, MCTSNode]:
        """Select the child node with the highest PUCB score."""
        best_score = -float("inf")
        best_move = None
        best_child = None

        # Calculate the sum of visit counts for all children
        total_visits = sum(child.visit_count for child in node.children.values())
        total_visits = max(total_visits, 1)  # Avoid division by zero

        for move, child in node.children.items():
            # Skip if this move would undo the last move
            if last_move and move == self._get_opposite_move(last_move):
                continue

            # PUCB score (combining predictor with upper confidence bound)
            exploit = child.get_mean_value() if child.visit_count > 0 else 0
            explore = (
                self.c_puct
                * child.prior
                * math.sqrt(self.epsilon + total_visits)
                / (1 + child.visit_count)
            )

            score = exploit + explore

            if score > best_score:
                best_score = score
                best_move = move
                best_child = child

        if best_move is None:
            # If all moves were invalid, choose any available move
            best_move, best_child = next(iter(node.children.items()))

        return best_move, best_child

    def _get_opposite_move(self, move: Move) -> Move:
        """Get the opposite move."""
        opposites = {
            Move.UP: Move.DOWN,
            Move.DOWN: Move.UP,
            Move.LEFT: Move.RIGHT,
            Move.RIGHT: Move.LEFT,
        }
        return opposites[move]

    def expand(
        self, node: MCTSNode, last_move: Optional[Move]
    ) -> tuple[bool, Optional[MCTSNode]]:
        """Expansion phase: add all possible child nodes to the leaf node."""
        board = node.board.copy()
        possible_next_boards = self.env.get_possible_next_boards()

        # Calculate prior probabilities using value network predictions
        prior_values = []
        next_boards = []
        goal_found = False
        goal_node = None

        for move, next_board in possible_next_boards.items():
            next_boards.append(next_board)

            if self.env.is_goal_board(next_board):
                goal_found = True
                goal_move = move
                next_board_copy = next_board.copy()
                goal_node = MCTSNode(
                    next_board_copy, parent=node, move=goal_move, prior=1.0
                )
                goal_node.value = 0.0  # Goal state has value 0
                node.children[goal_move] = goal_node
                break

            # Get value prediction
            value = self.agent.predict_distance(next_board).item()
            prior_values.append(-value)  # Negative because lower distance is better

        # If we found the goal, return early
        if goal_found:
            return True, goal_node

        # Convert to softmax probabilities
        if prior_values:
            prior_probs = F.softmax(torch.tensor(prior_values), dim=0).tolist()

            # Create child nodes with their priors
            for i, move in enumerate(possible_next_boards):
                child_node = MCTSNode(
                    next_boards[i], parent=node, move=move, prior=prior_probs[i]
                )
                child_node.value = -prior_values[
                    i
                ]  # Store the value network prediction
                node.children[move] = child_node

        return False, None

    def evaluate(self, node: MCTSNode) -> float:
        """Evaluation phase: get the value of the node from the value network."""
        return node.value

    def backpropagate(self, node: MCTSNode, value: float, path_length: int = 0):
        """Backpropagation phase: update the statistics for all nodes from leaf to root."""
        current = node
        while current is not None:
            current.visit_count += 1
            current.value_sum += value + path_length
            path_length += 1
            current = current.parent

    def run(self, board: IntBoard, last_move: Optional[Move] = None) -> Move:
        """Run the MCTS algorithm and return the best move."""
        self.initialize_search(board)

        for simulation_i in range(self.num_simulations):
            remaining_num_simulations = self.num_simulations - simulation_i
            children_visit_counts = sorted(
                (child.visit_count for child in self.root.children.values()),
                reverse=True,
            )
            if children_visit_counts:
                highest = children_visit_counts[0]
                next_highest = (
                    children_visit_counts[1] if len(children_visit_counts) > 1 else 0
                )
                if highest - next_highest > remaining_num_simulations:
                    break

            # 1. Selection phase - traverse the tree to a leaf node
            leaf, path_length, last_action = self.select_leaf(self.root, last_move)

            # If leaf is the goal state, backpropagate and continue
            if self.env.is_goal_board(leaf.board):
                self.backpropagate(leaf, 0, path_length)
                continue

            # 2. Expansion phase - expand the leaf node
            goal_found, goal_node = self.expand(leaf, last_action)

            # If expansion found the goal state, backpropagate and continue
            if goal_found:
                self.backpropagate(goal_node, 0, path_length + 1)
                continue

            # 3. Evaluation phase - use value network
            value = self.evaluate(leaf)

            # 4. Backpropagation phase
            self.backpropagate(leaf, value, path_length)

        return max(self.root.children.items(), key=lambda item: item[1].visit_count)[0]
