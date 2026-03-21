#!/bin/bash
# Quick start script for JAME Workflow

set -e

echo "🚀 Starting JAME Workflow..."
echo ""

# Check if we're in the right directory
if [ ! -d "backend" ] || [ ! -d "extension" ]; then
    echo "❌ Error: Please run this from the jame-workflow root directory"
    exit 1
fi

# Backend setup
echo "📦 Setting up backend..."
cd backend

if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

if ! pip list | grep -q fastapi; then
    echo "  Installing dependencies..."
    pip install -q -r requirements.txt
fi

# Check environment
if [ ! -f ".env" ]; then
    echo "  ⚠️  Copying .env template (edit with your credentials)..."
    cp .env.example .env
fi

# Validate backend
echo "  Validating backend setup..."
python validate.py

cd ..

# Extension setup (optional message)
echo ""
echo "✓ Backend ready!"
echo ""
echo "📝 Next steps:"
echo ""
echo "1️⃣  Start the backend API in terminal 1:"
echo "   cd backend && source .venv/bin/activate && python run_api.py"
echo ""
echo "2️⃣  In terminal 2, launch VS Code with extension:"
echo "   code --extensionDevelopmentPath=\"\$PWD/extension\""
echo ""
echo "3️⃣  Open JAME Workflow panel in VS Code sidebar and start building!"
echo ""
