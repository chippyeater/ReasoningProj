import type { TimelineView } from "../api";

export default function TimelineReasoning({ data }: { data: TimelineView }) {
  return (
    <section>
      <h3>{data.title}</h3>
      <ol>
        {data.events.map((event) => (
          <li key={event.id}>
            <time dateTime={event.sort_date ?? undefined}>{event.time_label}</time>
            <strong>{event.title}</strong>
            <p>{event.summary}</p>
            <small>{event.related_info_unit_ids.join(" · ")}</small>
          </li>
        ))}
      </ol>
    </section>
  );
}
