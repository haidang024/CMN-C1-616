"""AGENTIC STAR Marketplace entrypoint for CMN-C1-616."""

from shared.bootstrap.marketplace_app import run_agent_marketplace
from src.graph.graph import Graph


if __name__ == "__main__":
    run_agent_marketplace(Graph, agent_name="CMN-C1-616", namespace="agent1000")
