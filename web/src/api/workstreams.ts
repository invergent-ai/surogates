// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// The project routes (/v1/workstreams) as this page's signed-in user: the cards of a project's
// master, and in Surogate Desktop the projects this page serves.

import { FetchSseEventStream } from "@invergent/agent-chat-react";
import { saveFile } from "../lib/save-file";
import { authFetch } from "./auth";
import { workstreamRoutes } from "./workstream-routes";

// A version opened is a download this page starts: the browser, or Surogate Desktop, asks where to save it.
export const workstreams = workstreamRoutes(authFetch, (url, fetchFn) => new FetchSseEventStream(url, { fetchFn }), saveFile);
