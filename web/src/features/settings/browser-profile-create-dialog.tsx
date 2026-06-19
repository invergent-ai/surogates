import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Create with the typed name (empty string → the manager picks a default). */
  onCreate: (name: string) => Promise<void>;
}

export function BrowserProfileCreateDialog({
  open,
  onOpenChange,
  onCreate,
}: Props) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      await onCreate(name.trim());
      setName("");
      onOpenChange(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Set up your browser profile</DialogTitle>
        </DialogHeader>
        <div className="space-y-2">
          <label className="text-sm font-medium">Profile Name (optional)</label>
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g., Personal Account, Work Profile"
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
          />
          <p className="text-xs text-muted-foreground">
            Give your profile a name to easily identify it later.
          </p>
        </div>
        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={busy}
          >
            Cancel
          </Button>
          <Button onClick={submit} disabled={busy}>
            {busy ? "Creating…" : "Create Profile"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
