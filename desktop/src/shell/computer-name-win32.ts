// This computer's name on Windows, as the agent lists it and Settings shows it: its host name, which Node reads from
// Windows's sockets (GetHostNameW), the device name Settings → System → About shows and Rename this PC sets.

import { hostname } from "node:os";

export const computerName = (): string => hostname();
