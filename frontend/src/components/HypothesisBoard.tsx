import type { QuestionViewData } from "../api";

type Props = {
  data: QuestionViewData;
};

export default function HypothesisBoard({ data }: Props) {
  return (
    <section>
      <h3>Hypothesis Board</h3>
      {data.claims.length === 0 ? (
        <p>No claims available.</p>
      ) : (
        <ul>
          {data.claims.map((claim, idx) => (
            <li key={idx}>
              <pre>{JSON.stringify(claim, null, 2)}</pre>
            </li>
          ))}
        </ul>
      )}
      <h4>Evidence Paths</h4>
      <ul>
        {data.evidence_paths.map((path, idx) => (
          <li key={idx}>
            <pre>{JSON.stringify(path, null, 2)}</pre>
          </li>
        ))}
      </ul>
    </section>
  );
}
