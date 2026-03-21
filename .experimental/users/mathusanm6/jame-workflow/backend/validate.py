#!/usr/bin/env python3
"""Quick validation script to test backend and integration."""

import sys
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def test_imports():
    """Test that all dependencies are installed."""
    print("🔍 Testing imports...")
    try:
        import fastapi
        import langgraph
        import pydantic
        import yaml
        print("✓ All Core dependencies installed")
        return True
    except ImportError as e:
        print(f"✗ Missing dependency: {e}")
        return False


def test_prompts():
    """Test that prompts can be loaded."""
    print("\n🔍 Testing prompt loading...")
    sys.path.insert(0, os.path.dirname(__file__))
    
    try:
        from prompt_manager import PromptManager
        pm = PromptManager()
        
        agents = ["solution_architect", "software_engineer", "delivery_engineer", "quality_engineer"]
        for agent in agents:
            if agent in pm.prompts:
                print(f"✓ Loaded {agent}")
            else:
                print(f"✗ Missing {agent}")
                return False
        return True
    except Exception as e:
        print(f"✗ Failed to load prompts: {e}")
        return False


def test_llm():
    """Test that LLM provider can be initialized."""
    print("\n🔍 Testing LLM provider...")
    try:
        from llm_provider import get_llm
        llm = get_llm()
        print(f"✓ LLM initialized: {llm.model_name}")
        return True
    except Exception as e:
        print(f"✗ Failed to initialize LLM: {e}")
        return False


def test_agents():
    """Test that agents can be imported."""
    print("\n🔍 Testing agent implementations...")
    try:
        from agents.architect import architect_node, developer_node, delivery_engineer_node
        print("✓ Architect agent loaded")
        print("✓ Developer agent loaded")
        print("✓ Delivery agent loaded")
        return True
    except Exception as e:
        print(f"✗ Failed to load agents: {e}")
        return False


def test_graph():
    """Test that graph compiles."""
    print("\n🔍 Testing graph compilation...")
    try:
        from graph import build_graph
        graph = build_graph()
        print("✓ Graph compiled successfully")
        return True
    except Exception as e:
        print(f"✗ Failed to compile graph: {e}")
        return False


def main():
    print("=" * 50)
    print("  JAME Workflow Backend Validation")
    print("=" * 50)
    
    tests = [
        test_imports,
        test_prompts,
        test_llm,
        test_agents,
        test_graph,
    ]
    
    results = [test() for test in tests]
    
    print("\n" + "=" * 50)
    if all(results):
        print("✓ All checks passed! Backend is ready.")
        print("\nStart the API with:")
        print("  python run_api.py")
        return 0
    else:
        print("✗ Some checks failed. See details above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
