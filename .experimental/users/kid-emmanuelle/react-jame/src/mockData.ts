import type { RuleItem, AgentItem, McpItem } from './types';

export const MOCK_RULES: RuleItem[] = [
  { name: 'typescript', slug: 'typescript', display_name: 'TypeScript', type: 'rule' },
  { name: 'python', slug: 'python', display_name: 'Python', type: 'rule' },
  { name: 'react', slug: 'react', display_name: 'React Guidelines', type: 'rule' },
  { name: 'api-design', slug: 'api-design', display_name: 'API Design', type: 'rule' },
  { name: 'testing', slug: 'testing', display_name: 'Testing', type: 'rule' },
  { name: 'git-workflow', slug: 'git-workflow', display_name: 'Git Workflow', type: 'rule' },
];

export const MOCK_AGENTS: AgentItem[] = [
  { name: 'researcher', slug: 'researcher', display_name: 'Researcher', filename: 'researcher.md' },
  { name: 'code-reviewer', slug: 'code-reviewer', display_name: 'Code Reviewer', filename: 'code-reviewer.md' },
  { name: 'memory-manager', slug: 'memory-manager', display_name: 'Memory Manager', filename: 'memory-manager.md' },
  { name: 'debugger', slug: 'debugger', display_name: 'Debugger', filename: 'debugger.md' },
];

export const MOCK_MCPS: McpItem[] = [
  { name: 'github', display_name: 'GitHub MCP' },
  { name: 'supabase', display_name: 'Supabase MCP' },
  { name: 'database', display_name: 'Database MCP' },
  { name: 'filesystem', display_name: 'Filesystem MCP' },
];

const RULE_CONTENTS: Record<string, string> = {
  typescript: `# TypeScript Coding Rules

## Type Safety
- Always use strict mode
- No \`any\` type unless absolutely necessary
- Define interfaces for all data structures

## Code Style
- Use Prettier for formatting
- Prefer const over let
- Use arrow functions for callbacks
- Avoid implicit any by always annotating function parameters`,

  python: `# Python Style Guidelines

## Code Formatting
- Use PEP 8 style guide
- Line length: 88 characters (Black formatter)
- Use type hints for all functions

## Best Practices
- Use meaningful variable names
- Add docstrings to all functions and classes
- Prefer list comprehensions over loops when appropriate
- Use f-strings for string formatting`,

  react: `# React Development Guidelines

## Component Structure
- Use functional components with hooks
- Prefer composition over inheritance
- Keep components small and focused

## State Management
- Use useState for local state
- Use useContext for shared state
- Consider useReducer for complex state logic

## Performance
- Use React.memo for expensive components
- Optimize re-renders with useMemo and useCallback`,

  'api-design': `# API Design Guidelines

## RESTful Principles
- Use HTTP methods correctly (GET, POST, PUT, DELETE)
- Use meaningful resource URLs
- Return appropriate HTTP status codes

## Request/Response Format
- Use JSON for request and response bodies
- Include proper Content-Type headers
- Implement consistent error response format`,

  testing: `# Testing Guidelines

## Unit Tests
- Test one thing per test
- Use descriptive test names
- Mock external dependencies

## Coverage
- Aim for 80%+ code coverage
- Focus on critical business logic
- Write tests before fixing bugs`,

  'git-workflow': `# Git Workflow

## Branches
- Use feature branches for all changes
- Name branches: feature/, fix/, chore/
- Delete branches after merging

## Commits
- Write clear, imperative commit messages
- Keep commits small and focused
- Reference issues in commit messages`,
};

const AGENT_CONTENTS: Record<string, string> = {
  researcher: `---
name: researcher
description: Specialized subagent for information gathering and analysis
---

# Researcher Agent

A specialized subagent for information gathering and analysis.

## Capabilities
- Web search and analysis
- Document research
- Data compilation and synthesis

## Usage
\`\`\`
@researcher find information about [topic]
\`\`\``,

  'code-reviewer': `---
name: code-reviewer
description: Specialized agent for code review and quality assessment
---

# Code Reviewer Agent

Specialized agent for code review and quality assessment.

## Capabilities
- Code quality analysis
- Security vulnerability detection
- Best practices compliance

## Usage
\`\`\`
@code-reviewer analyze this code for issues
\`\`\``,

  'memory-manager': `---
name: memory-manager
description: Handles context and conversation history management
---

# Memory Manager Agent

Handles context and conversation history management.

## Features
- Long-term context preservation
- Conversation summarization
- Knowledge base integration

## Usage
\`\`\`
@memory-manager store: [information]
@memory-manager recall: [topic]
\`\`\``,

  debugger: `---
name: debugger
description: Expert at diagnosing and fixing bugs
---

# Debugger Agent

Expert at diagnosing and fixing bugs across the codebase.

## Capabilities
- Root cause analysis
- Stack trace interpretation
- Fix suggestions with explanations

## Usage
\`\`\`
@debugger investigate this error: [error message]
\`\`\``,
};

const MCP_CONFIGS: Record<string, object> = {
  github: {
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-github'],
    env: { GITHUB_PERSONAL_ACCESS_TOKEN: '${GITHUB_TOKEN}' },
  },
  supabase: {
    command: 'npx',
    args: ['-y', '@supabase/mcp-server-supabase@latest'],
    env: { SUPABASE_URL: '${SUPABASE_URL}', SUPABASE_KEY: '${SUPABASE_KEY}' },
  },
  database: {
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-postgres'],
    env: { DATABASE_URL: '${DATABASE_URL}' },
  },
  filesystem: {
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-filesystem', '/path/to/project'],
  },
};

export function getRuleContent(slug: string): string {
  return RULE_CONTENTS[slug] || `# ${slug}\n\nAdd your rules here.`;
}

export function getAgentContent(slug: string): { filename: string; content: string } {
  const agent = MOCK_AGENTS.find((a) => a.slug === slug || a.name === slug);
  const filename = agent?.filename || `${slug}.md`;
  return { filename, content: AGENT_CONTENTS[slug] || `# ${slug} Agent\n\nDescribe your agent here.` };
}

export function getMcpConfig(name: string): object {
  return MCP_CONFIGS[name] || { command: 'npx', args: ['-y', `@mcp/${name}`] };
}

export function getMockRecommendations(repoUrl: string): {
  rules: string[];
  agents: string[];
  mcps: string[];
} {
  // Deterministic fake recommendations based on URL content
  const url = repoUrl.toLowerCase();
  const rules = ['typescript', 'api-design'];
  const agents = ['code-reviewer'];
  const mcps: string[] = ['github'];

  if (url.includes('python') || url.includes('django') || url.includes('flask')) {
    rules[0] = 'python';
  }
  if (url.includes('react') || url.includes('next') || url.includes('vue')) {
    rules.push('react');
    agents.push('researcher');
  }
  if (url.includes('supabase') || url.includes('postgres') || url.includes('db')) {
    mcps.push('supabase');
  }

  return { rules, agents, mcps };
}
