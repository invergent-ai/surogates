// This computer's name on Linux, as the agent lists it and Settings shows it: its host name.

import { hostname } from "node:os";

export const computerName = (): string => hostname();
