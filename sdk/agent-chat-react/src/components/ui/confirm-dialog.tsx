import { Loader2 } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Button } from "./button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  confirmIcon?: ReactNode;
  variant?: "destructive" | "default";
  /** Disables the confirm button. Use when the action is known to be
   *  invalid (e.g. an in-use entity) and the dialog only exists to
   *  explain why. */
  confirmDisabled?: boolean;
  onConfirm: () => Promise<void> | void;
  onCancel: () => void;
}

/**
 * Modal confirmation dialog. Mirrors the convention used by the
 * surogate-ops Studio frontend so dialogs look identical regardless of
 * which side renders them. The variant prop defaults to "destructive"
 * because this component is overwhelmingly used to gate irreversible
 * actions; pass "default" when confirming a benign choice.
 */
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
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next && !loading) onCancel();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" disabled={loading} onClick={onCancel}>
            Cancel
          </Button>
          <Button
            variant={variant}
            disabled={loading || confirmDisabled}
            onClick={() => void handleConfirm()}
          >
            {loading ? (
              <Loader2 className="mr-1.5 size-3.5 animate-spin" aria-hidden="true" />
            ) : (
              confirmIcon
            )}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
