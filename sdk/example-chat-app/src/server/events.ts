import type { Response } from "express";
import type {
  ExampleEventType,
  ExampleRuntimeEvent,
} from "../shared/types";

export type EventSubscriber = (event: ExampleRuntimeEvent) => void;

export class SessionEventLog {
  private nextEventId = 1;
  private readonly events: ExampleRuntimeEvent[] = [];
  private readonly subscribers = new Set<EventSubscriber>();

  append(type: ExampleEventType, data: Record<string, unknown> = {}) {
    const event = { type, eventId: this.nextEventId++, data };
    this.events.push(event);
    for (const subscriber of this.subscribers) subscriber(event);
    return event;
  }

  replay(after = 0) {
    return this.events.filter((event) => event.eventId > after);
  }

  subscribe(subscriber: EventSubscriber) {
    this.subscribers.add(subscriber);
    return () => {
      this.subscribers.delete(subscriber);
    };
  }
}

export function writeSseEvent(response: Response, event: ExampleRuntimeEvent) {
  response.write(`id: ${event.eventId}\n`);
  response.write(`event: ${event.type}\n`);
  response.write(`data: ${JSON.stringify(event.data)}\n\n`);
}
