import type { HypothesisView } from "../api";

export default function HypothesisBoard({ data }: { data: HypothesisView }) {
  return (
    <section>
      <h3>{data.title}</h3>
      <p>{data.hypothesis}</p>
      <ol>
        {data.steps.map((step) => (
          <li key={step.id}>
            <strong>{step.claim}</strong>
            <p>支持：{step.supporting_info_unit_ids.join("、") || "无"}</p>
            <p>反对：{step.opposing_info_unit_ids.join("、") || "无"}</p>
            {step.depends_on_step_ids.length > 0 && <small>依赖：{step.depends_on_step_ids.join("、")}</small>}
          </li>
        ))}
      </ol>
      {data.missing_information.length > 0 && <p>待补：{data.missing_information.join("；")}</p>}
    </section>
  );
}
