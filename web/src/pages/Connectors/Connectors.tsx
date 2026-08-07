import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useScrollMemory } from '@/lib/scrollMemory';
import { toast } from '@/components/ui/use-toast';
import { ConnectorServers } from './ConnectorServers';
import { ConnectorSecrets } from './ConnectorSecrets';
import './Connectors.css';

/**
 * /connectors — user-level MCP servers + user vault. An enabled server here is
 * inherited by every workspace of the user; OAuth-connected servers are bound
 * into sandboxes through the egress relay (credentials never leave the host).
 *
 * Also the landing route of the OAuth connect flow: the backend callback
 * redirects here with `?mcp_connected=<server>` or `?mcp_error=<reason>&server=`
 * — surfaced as a toast, then stripped from the URL.
 */

const TABS = ['servers', 'secrets'] as const;
type Tab = (typeof TABS)[number];

function Connectors() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { t } = useTranslation();

  const tabParam = searchParams.get('tab');
  const [activeTab, setActiveTab] = useState<Tab>(
    TABS.includes(tabParam as Tab) ? (tabParam as Tab) : 'servers',
  );
  const pageRef = useRef<HTMLDivElement>(null);
  useScrollMemory(pageRef, 'page:connectors');

  const handleTabChange = (tab: Tab) => {
    setActiveTab(tab);
    setSearchParams({ tab }, { replace: true });
  };

  // Sync from URL on back/forward navigation
  useEffect(() => {
    const urlTab = searchParams.get('tab');
    if (urlTab && TABS.includes(urlTab as Tab) && urlTab !== activeTab) {
      setActiveTab(urlTab as Tab);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  // OAuth callback landing: toast the outcome once, then strip the params so a
  // refresh doesn't re-announce it.
  const callbackHandled = useRef(false);
  useEffect(() => {
    if (callbackHandled.current) return;
    const connected = searchParams.get('mcp_connected');
    const errorReason = searchParams.get('mcp_error');
    if (!connected && !errorReason) return;
    callbackHandled.current = true;
    if (connected) {
      toast({
        title: t('connectors.oauth.connectedTitle'),
        description: t('connectors.oauth.connectedDesc', { server: connected }),
      });
    } else {
      const server = searchParams.get('server');
      toast({
        variant: 'destructive',
        title: t('connectors.oauth.callbackErrorTitle'),
        description: server ? `${server}: ${errorReason}` : String(errorReason),
      });
    }
    const next = new URLSearchParams(searchParams);
    next.delete('mcp_connected');
    next.delete('mcp_error');
    next.delete('server');
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  return (
    <div ref={pageRef} className="connectors-page">
      <div className="connectors-container">
        <h2 className="text-xl font-semibold mb-1" style={{ color: 'var(--color-text-primary)' }}>
          {t('connectors.title')}
        </h2>
        <p className="text-sm mb-6" style={{ color: 'var(--color-text-tertiary)' }}>
          {t('connectors.description')}
        </p>
        <div className="flex gap-2 mb-6 border-b overflow-x-auto connectors-tab-bar" style={{ borderColor: 'var(--color-border-muted)' }}>
          {TABS.map((tab) => (
            <button
              key={tab}
              type="button"
              onClick={() => handleTabChange(tab)}
              className="px-4 py-2 text-sm font-medium whitespace-nowrap flex-shrink-0"
              style={{
                color: activeTab === tab ? 'var(--color-text-primary)' : 'var(--color-text-tertiary)',
                borderBottom: activeTab === tab ? '2px solid var(--color-accent-primary)' : '2px solid transparent',
              }}
            >
              {t(`connectors.tabs.${tab}`)}
            </button>
          ))}
        </div>

        <div className="connectors-content">
          {activeTab === 'servers' && <ConnectorServers />}
          {activeTab === 'secrets' && <ConnectorSecrets />}
        </div>
      </div>
    </div>
  );
}

export default Connectors;
