import type { ReasonResponse } from "../api";

type Props = {
  data: ReasonResponse;
};

// Render timeline-focused view when backend recommends timeline_reasoning.
export default function TimelineReasoning({ data }: Props) {
  return (
    <section>
      <h3>Timeline Reasoning</h3>
      {data.events.length === 0 ? (
        <p>No events available.</p>
      ) : (
        <ol>
          {data.events.map((event, idx) => (
            <li key={idx}>
              <pre>{JSON.stringify(event, null, 2)}</pre>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
