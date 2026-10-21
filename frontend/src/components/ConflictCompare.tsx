import type { ConflictView } from "../api";

export default function ConflictCompare({ data }: { data: ConflictView }) {
  return (
    <section>
      <h3>{data.title}</h3>
      <div>
        {data.sections.map((section) => (
          <section key={section.id} data-placement={section.placement}>
            <h4>{section.title}</h4>
            <ul>
              {section.items.map((item) => (
                <li key={`${section.id}-${item.info_unit_id}`}>
                  <span>{item.role}</span> {item.info_unit_id}
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
      {data.missing_information.length > 0 && <p>待补：{data.missing_information.join("；")}</p>}
    </section>
  );
}
