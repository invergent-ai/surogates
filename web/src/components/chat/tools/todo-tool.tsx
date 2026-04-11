// Copyright (c) 2026, Invergent SA, developed by Flavius Burca
// SPDX-License-Identifier: AGPL-3.0-only
//
import { Loader2Icon, ListTodoIcon } from "lucide-react";
import {
  Queue,
  QueueItem,
  QueueItemContent,
  QueueItemDescription,
  QueueItemIndicator,
  QueueList,
  QueueSection,
  QueueSectionContent,
  QueueSectionLabel,
  QueueSectionTrigger,
} from "@/components/ai-elements/queue";
import type { ToolCallInfo } from "@/hooks/use-session-runtime";

interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
  description?: string;
}

export function parseTodoResult(result: string | undefined): TodoItem[] | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result);
    const todos = parsed?.todos ?? parsed;
    if (!Array.isArray(todos)) return null;
    if (todos.length === 0) return null;
    if (!todos.every((t: unknown) =>
      typeof t === "object" && t !== null &&
      "id" in t && "content" in t && "status" in t
    )) return null;
    return todos as TodoItem[];
  } catch {
    return null;
  }
}

export function TodoToolBlock({ tc }: { tc: ToolCallInfo }) {
  const todos = parseTodoResult(tc.result);
  if (!todos) return null;

  const completed = todos.filter((t) => t.status === "completed").length;
  const total = todos.length;

  return (
    <Queue>
      <QueueSection>
        <QueueSectionTrigger>
          <QueueSectionLabel
            count={total}
            label={`tasks (${completed}/${total} done)`}
            icon={<ListTodoIcon className="size-3.5" />}
          />
        </QueueSectionTrigger>
        <QueueSectionContent>
          <QueueList>
            {todos.map((todo) => {
              const isCompleted = todo.status === "completed";
              const isInProgress = todo.status === "in_progress";
              return (
                <QueueItem key={todo.id}>
                  <div className="flex items-center gap-2">
                    {isInProgress ? (
                      <Loader2Icon className="size-2.5 animate-spin text-primary shrink-0" />
                    ) : (
                      <QueueItemIndicator completed={isCompleted} />
                    )}
                    <QueueItemContent completed={isCompleted}>
                      {todo.content}
                    </QueueItemContent>
                  </div>
                  {todo.description && (
                    <QueueItemDescription completed={isCompleted}>
                      {todo.description}
                    </QueueItemDescription>
                  )}
                </QueueItem>
              );
            })}
          </QueueList>
        </QueueSectionContent>
      </QueueSection>
    </Queue>
  );
}
