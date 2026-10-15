// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "./button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "./dialog";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: React.ReactNode;
  confirmLabel?: string;
  confirmIcon?: React.ReactNode;
  variant?: "destructive" | "default";
  /** Disables the confirm button. Use when the action is known
   *  to be invalid (e.g. an in-use entity) and the dialog only
   *  exists to explain why. */
  confirmDisabled?: boolean;
  onConfirm: () => Promise<void> | void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "Confirm",
  confirmIcon,
  variant = "destructive",
  confirmDisabled = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const [loading, setLoading] = useState(false);

  const handleConfirm = async () => {
    setLoading(true);
    try {
      await onConfirm();
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o && !loading) onCancel(); }}>
      {/* Work's confirm: compact, no close button — Cancel and Escape do it. */}
      <DialogContent
        showCloseButton={false}
        className="gap-3.5 px-6 pt-6 pb-5 sm:max-w-[430px]"
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter className="pt-1.5">
          <Button
            variant="outline"
            disabled={loading}
            onClick={onCancel}
          >
            Cancel
          </Button>
          <Button
            variant={variant}
            disabled={loading || confirmDisabled}
            onClick={handleConfirm}
          >
            {loading ? <Loader2 className="animate-spin mr-1.5" size={14} /> : confirmIcon}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
