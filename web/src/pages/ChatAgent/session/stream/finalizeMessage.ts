type MessageEnd = 'completed' | 'stopped' | 'failed' | 'paused' | 'disconnected';

/** Ending a client stream is not proof of server settlement. Only terminal
 * observations get a local clock; replay's completedAt remains authoritative. */
export function finalizeAssistantMessage<T extends object>(
  message: T,
  end: MessageEnd,
  observedAt = Date.now(),
): T & { isStreaming: false; completionObservedAt?: number } {
  const previous = 'completionObservedAt' in message ? message.completionObservedAt : undefined;
  const terminal = end === 'completed' || end === 'stopped' || end === 'failed';
  return {
    ...message,
    isStreaming: false,
    completionObservedAt: terminal
      ? typeof previous === 'number' ? previous : observedAt
      : undefined,
    ...(end === 'stopped' ? { stopped: true } : {}),
    ...(end === 'failed' ? { error: true } : {}),
  };
}
