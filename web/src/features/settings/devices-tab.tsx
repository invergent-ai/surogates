// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
// Settings → Devices (desktop design, Section 8): the user's computers that can work on their
// folders through this agent, each with when it was added, reauthorized and last seen, and
// Revoke; and the sign-ins of Surogate Desktop, each with End. In the browser and the desktop.
import { LaptopIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

import { type Device, endSignIn, listDevices, listSignIns, revokeDevice, type SignIn } from "@/api/devices";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { getDesktop } from "@/lib/desktop-bridge";
import { deviceHistory, deviceState } from "@/lib/devices";

export function DevicesTab() {
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [signIns, setSignIns] = useState<SignIn[] | "sign-in-again" | null>(null);
  // In Surogate Desktop, the computer this window runs on.
  const [thisComputer, setThisComputer] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<Device | null>(null);
  const [ending, setEnding] = useState<SignIn | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  const load = useCallback(() => {
    const failed = (error: unknown) => setFailure(error instanceof Error ? error.message : String(error));
    void listDevices().then(setDevices, failed);
    void listSignIns().then(setSignIns, failed);
  }, []);

  useEffect(() => {
    load();
    void getDesktop()?.getDevice().then((state) => setThisComputer(state.device?.deviceId ?? null), () => {});
  }, [load]);

  const act = async (action: () => Promise<void>) => {
    setFailure(null);
    try {
      await action();
    } catch (error) {
      setFailure(error instanceof Error ? error.message : String(error));
    }
    load();
  };
  // Revoking this computer, or ending the sign-in it was added with, signs Surogate out here too.
  const here = "Surogate on this computer signs out too.";

  return (
    <div className="space-y-8">
      <section className="space-y-3" aria-labelledby="computers-heading">
        <h2 id="computers-heading" className="text-sm font-semibold">Computers</h2>
        <p className="text-sm leading-relaxed text-muted-foreground">
          Computers running Surogate Desktop that this agent can work on folders of. Revoking one
          stops its work at once; it works on no folder until it is restored on that computer.
        </p>
        {devices?.length === 0 && <p className="text-sm text-muted-foreground">No computers yet.</p>}
        {devices !== null && devices.length > 0 && (
          <ul className="divide-y divide-line rounded-xl border border-line">
            {devices.map((device) => (
              <li key={device.id} className="flex items-center gap-3 px-4 py-3">
                <LaptopIcon className="size-5 shrink-0 text-muted-foreground" aria-hidden="true" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 font-medium">
                    <span className="truncate">{device.name}</span>
                    {device.id === thisComputer && <Badge variant="secondary">This computer</Badge>}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    {deviceHistory(device)} · {deviceState(device)}
                  </div>
                </div>
                {!device.revoked_at && (
                  <Button variant="outline" size="sm" aria-label={`Revoke ${device.name}`} onClick={() => setRevoking(device)}>
                    Revoke
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-3" aria-labelledby="sign-ins-heading">
        <h2 id="sign-ins-heading" className="text-sm font-semibold">Surogate Desktop sign-ins</h2>
        {signIns === "sign-in-again" ? (
          <p className="text-sm text-muted-foreground">
            Sign in again to see your desktop sign-ins: the agent shows them only to a sign-in from the last 10 minutes.
          </p>
        ) : signIns?.length === 0 ? (
          <p className="text-sm text-muted-foreground">No desktop sign-ins.</p>
        ) : signIns !== null && signIns.length > 0 && (
          <ul className="divide-y divide-line rounded-xl border border-line">
            {signIns.map((signIn) => (
              <li key={signIn.id} className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 font-medium">
                    <span className="truncate">{signIn.device_name ?? "No computer added"}</span>
                    {signIn.device_id !== null && signIn.device_id === thisComputer && <Badge variant="secondary">This computer</Badge>}
                  </div>
                  <div className="text-xs text-muted-foreground">
                    Signed in {new Date(signIn.created_at).toLocaleString()} · last used {new Date(signIn.last_used_at).toLocaleString()}
                  </div>
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  aria-label={`End the sign-in ${signIn.device_name ? `of ${signIn.device_name}` : "with no computer added"}, made ${new Date(signIn.created_at).toLocaleString()}`}
                  onClick={() => setEnding(signIn)}
                >
                  End
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <p role="alert" className="text-sm text-destructive">
        {failure}
      </p>

      <ConfirmDialog
        open={revoking !== null}
        title={revoking ? `Revoke ${revoking.name}?` : "Revoke this computer?"}
        description={`It stops working on your folders at once, and what it was doing is cancelled. Its chats say Local access revoked until you restore it in Surogate on that computer.${revoking?.id === thisComputer ? ` ${here}` : ""}`}
        confirmLabel="Revoke"
        variant="destructive"
        onConfirm={async () => {
          const device = revoking;
          setRevoking(null);
          if (device) await act(() => revokeDevice(device.id));
        }}
        onCancel={() => setRevoking(null)}
      />
      <ConfirmDialog
        open={ending !== null}
        title="End this sign-in?"
        description={`Surogate Desktop signed in this way signs out, and so does its window.${ending?.device_id !== null && ending?.device_id === thisComputer ? ` ${here}` : ""}`}
        confirmLabel="End"
        variant="destructive"
        onConfirm={async () => {
          const signIn = ending;
          setEnding(null);
          if (signIn) await act(() => endSignIn(signIn.id));
        }}
        onCancel={() => setEnding(null)}
      />
    </div>
  );
}
