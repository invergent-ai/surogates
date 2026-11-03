// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The host's tool labels, handed from AgentChat to every row that names a
// tool call. A thread drawn outside AgentChat names its tools in the SDK's
// own words.

import { createContext, useContext } from "react";
import type { AgentChatToolLabels } from "../../types";

export const NO_TOOL_LABELS: AgentChatToolLabels = {};

const ToolLabelsContext = createContext<AgentChatToolLabels>(NO_TOOL_LABELS);

export const ToolLabelsProvider = ToolLabelsContext.Provider;

export function useToolLabels(): AgentChatToolLabels {
  return useContext(ToolLabelsContext);
}
