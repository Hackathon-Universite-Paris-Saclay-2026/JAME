import { useState, useCallback } from 'react';
import { useWorkspace } from '../context/WorkspaceContext';
import { MOCK_RULES, MOCK_AGENTS, MOCK_MCPS, getRuleContent, getAgentContent, getMcpConfig } from '../mockData';
import type { RuleItem, AgentItem, McpItem } from '../types';

type Category = 'mcps' | 'rules' | 'agents';

interface MenuSectionProps {
  label: string;
  theme: 'yellow' | 'pink' | 'blue';
  items: (RuleItem | AgentItem | McpItem)[];
  isExpanded: boolean;
  isActive: boolean;
  checkedItems: Record<string, boolean>;
  searchQuery: string;
  onToggleExpanded: () => void;
  onToggleItem: (name: string) => void;
}

function MenuSection({
  label,
  theme,
  items,
  isExpanded,
  isActive,
  checkedItems,
  searchQuery,
  onToggleExpanded,
  onToggleItem,
}: MenuSectionProps) {
  const themeColors: Record<string, string> = {
    yellow: '#FDE047',
    pink: '#FFC480',
    blue: '#a7cefd',
  };
  const color = themeColors[theme];

  const buttonStyle = isActive ? { background: color } : { background: 'white' };
  const plusStyle = isActive
    ? { background: 'white', borderRight: '2px solid black' }
    : { background: color, borderRight: '2px solid black' };

  const filteredItems = searchQuery
    ? items.filter((item) => {
        const name = ((item as RuleItem).display_name || item.name).toLowerCase();
        return name.includes(searchQuery.toLowerCase());
      })
    : items;

  const hidden = searchQuery && filteredItems.length === 0;

  return (
    <div className={`menu-item mb-2 ${hidden ? 'hidden' : ''}`}>
      <button
        className={`menu-button expandable ${isActive ? 'toggled' : ''} ${isExpanded ? 'expanded' : ''}`}
        data-theme={theme}
        onClick={onToggleExpanded}
        style={buttonStyle}
      >
        <span className="menu-button-plus" style={plusStyle}>+</span>
        <span className="menu-button-text">{label}</span>
        <span className="menu-button-expand" style={{ transform: isExpanded ? 'rotate(180deg)' : 'rotate(0deg)' }}>▼</span>
      </button>
      {isExpanded && (
        <div className="menu-dropdown">
          <div className="p-2">
            {filteredItems.length === 0 && (
              <div className="text-xs text-gray-500">No {label.toLowerCase()} available</div>
            )}
            {filteredItems.map((item) => {
              const id = (item as RuleItem).slug || item.name;
              const displayName = (item as RuleItem).display_name || item.name;
              const isChecked = !!checkedItems[id];
              const isHighlighted = searchQuery && displayName.toLowerCase().includes(searchQuery.toLowerCase());
              return (
                <div
                  key={id}
                  className={`dropdown-item ${isChecked ? 'active' : ''} ${isHighlighted ? 'search-match' : ''}`}
                  onClick={() => onToggleItem(id)}
                >
                  <span className={`dropdown-item-icon ${isChecked ? 'bg-black text-white' : ''}`}>
                    {isChecked ? '✓' : '+'}
                  </span>
                  <span>{displayName}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

export default function WorkspaceActions() {
  const {
    workspaceState,
    includeFile,
    deleteFile,
    setActionCategory,
    setActionItem,
    setAgentMapping,
  } = useWorkspace();

  const { actionStates, files, agentMappings } = workspaceState;
  const [searchQuery, setSearchQuery] = useState('');

  function toggleExpanded(category: Category) {
    const current = actionStates[category];
    setActionCategory(category, current.active, !current.expanded);
  }

  const handleToggleMcp = useCallback((mcpName: string) => {
    const isChecked = actionStates.mcps.items[mcpName] || false;
    if (!isChecked) {
      let currentConfig: Record<string, unknown> = {};
      const existingFile = files['.mcp.json'];
      if (existingFile) {
        try { currentConfig = JSON.parse(existingFile); } catch { currentConfig = {}; }
      }
      if (!currentConfig.mcpServers) currentConfig.mcpServers = {};
      (currentConfig.mcpServers as Record<string, unknown>)[mcpName] = getMcpConfig(mcpName);
      includeFile('.mcp.json', JSON.stringify(currentConfig, null, 2));
      setActionItem('mcps', mcpName, true);
      setActionCategory('mcps', true, true);
    } else {
      let currentConfig: Record<string, unknown> = {};
      const existingFile = files['.mcp.json'];
      if (existingFile) {
        try { currentConfig = JSON.parse(existingFile); } catch { currentConfig = {}; }
      }
      if (currentConfig.mcpServers) {
        delete (currentConfig.mcpServers as Record<string, unknown>)[mcpName];
        const remaining = Object.keys(currentConfig.mcpServers as Record<string, unknown>).length;
        if (remaining === 0) {
          deleteFile('.mcp.json');
        } else {
          includeFile('.mcp.json', JSON.stringify(currentConfig, null, 2));
        }
      }
      setActionItem('mcps', mcpName, false);
      const remainingChecked = Object.entries(actionStates.mcps.items).filter(([k, v]) => k !== mcpName && v).length;
      if (remainingChecked === 0) setActionCategory('mcps', false, true);
    }
  }, [actionStates.mcps.items, files, includeFile, deleteFile, setActionItem, setActionCategory]);

  const handleToggleAgent = useCallback((agentName: string) => {
    const isChecked = actionStates.agents.items[agentName] || false;
    if (!isChecked) {
      const { filename, content } = getAgentContent(agentName);
      const agentPath = `.claude/agents/${filename}`;
      setAgentMapping(agentName, agentPath);
      includeFile(agentPath, content);
      setActionItem('agents', agentName, true);
      setActionCategory('agents', true, true);
    } else {
      const agentPath = agentMappings[agentName];
      if (agentPath && files[agentPath]) deleteFile(agentPath);
      setActionItem('agents', agentName, false);
      const remainingChecked = Object.entries(actionStates.agents.items).filter(([k, v]) => k !== agentName && v).length;
      if (remainingChecked === 0) setActionCategory('agents', false, true);
    }
  }, [actionStates.agents.items, files, agentMappings, includeFile, deleteFile, setActionItem, setActionCategory, setAgentMapping]);

  const handleToggleRule = useCallback((ruleName: string) => {
    const isChecked = actionStates.rules.items[ruleName] || false;
    if (!isChecked) {
      const currentContent = files['CLAUDE.md'] || '';
      const ruleContent = getRuleContent(ruleName).trim();
      const newContent = currentContent + (currentContent ? '\n\n' : '') + ruleContent;
      includeFile('CLAUDE.md', newContent);
      setActionItem('rules', ruleName, true);
      setActionCategory('rules', true, true);
    } else {
      setActionItem('rules', ruleName, false);
      const remainingChecked = Object.entries(actionStates.rules.items).filter(([k, v]) => k !== ruleName && v).length;
      if (remainingChecked === 0) setActionCategory('rules', false, true);
    }
  }, [actionStates.rules.items, files, includeFile, setActionItem, setActionCategory]);

  return (
    <div className="workspace-actions" style={{ height: '100%', display: 'flex', flexDirection: 'column' }}>
      <div className="p-4 flex-1 overflow-y-auto">
        <h3 className="text-lg font-black text-black mb-4">Add tools</h3>

        {/* Search */}
        <div className="mb-4">
          <div className="search-container">
            <input
              type="text"
              className="search-input"
              placeholder="Search actions, tags..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
            {searchQuery && (
              <div className="search-clear" onClick={() => setSearchQuery('')}>×</div>
            )}
          </div>
        </div>

        {/* Actions */}
        <div className="mb-4">
          <h4 className="text-sm font-bold text-black mb-2">Actions</h4>

          <MenuSection
            label="MCPs"
            theme="yellow"
            items={MOCK_MCPS}
            isExpanded={actionStates.mcps.expanded}
            isActive={actionStates.mcps.active}
            checkedItems={actionStates.mcps.items}
            searchQuery={searchQuery}
            onToggleExpanded={() => toggleExpanded('mcps')}
            onToggleItem={handleToggleMcp}
          />

          <MenuSection
            label="Rules"
            theme="pink"
            items={MOCK_RULES}
            isExpanded={actionStates.rules.expanded}
            isActive={actionStates.rules.active}
            checkedItems={actionStates.rules.items}
            searchQuery={searchQuery}
            onToggleExpanded={() => toggleExpanded('rules')}
            onToggleItem={handleToggleRule}
          />

          <MenuSection
            label="Agents"
            theme="blue"
            items={MOCK_AGENTS}
            isExpanded={actionStates.agents.expanded}
            isActive={actionStates.agents.active}
            checkedItems={actionStates.agents.items}
            searchQuery={searchQuery}
            onToggleExpanded={() => toggleExpanded('agents')}
            onToggleItem={handleToggleAgent}
          />
        </div>
      </div>
    </div>
  );
}
