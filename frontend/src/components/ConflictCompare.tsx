import type { ReasonResponse } from "../api";

type Props = {
  data: ReasonResponse;
};

// Render conflict-focused view when backend recommends conflict_compare.
export default function ConflictCompare({ data }: Props) {
  return (
    <section>
      <h3>Conflict Compare</h3>
      {data.conflicts.length === 0 ? (
        <p>No conflicts detected.</p>
      ) : (
        <ul>
          {data.conflicts.map((item, idx) => (
            <li key={idx}>
              <pre>{JSON.stringify(item, null, 2)}</pre>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
