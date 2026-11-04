// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Settings → Connected Accounts: the services the agent can use on the user's behalf (Outlook,
// OneDrive, ...), each connected or disconnected here. The same list the chat's "Connect your
// accounts" band opens, and the place that band's hide button points to.
import { IntegrationsPage } from "@invergent/agent-chat-react";

import { surogatesWebChatAdapter } from "@/features/chat";

export function ConnectedAccountsTab() {
  return (
    <div>
      <div className="mb-6 min-w-0">
        <h2 className="text-base font-semibold text-foreground">Connected Accounts</h2>
        <p className="text-sm text-muted-foreground">
          Services the agent can use on your behalf. Each connection is yours alone.
        </p>
      </div>
      <IntegrationsPage adapter={surogatesWebChatAdapter} embedded />
    </div>
  );
}
