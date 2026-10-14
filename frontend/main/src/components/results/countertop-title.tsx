import type { CountertopResult } from '@/api/client';
import { distinguishingLabel, type CountertopItem } from '@/lib/countertop-results';

type Row = Pick<CountertopResult, 'row_id' | 'page_number' | 'label'>;
type Siblings = readonly Pick<CountertopResult, 'row_id' | 'page_number'>[];

/** The label's numbers in the number face ("row 3.1": 3.1 in Plex Mono), its words in Geist. */
function Words({ text }: { text: string }) {
  return (
    <>
      {text.split(/(\d+(?:\.\d+)*)/).map((part, index) =>
        index % 2 === 1 ? <span key={index} className="num">{part}</span> : part,
      )}
    </>
  );
}

/**
 * A countertop's name where nothing beside it says the page (#1155): "Countertop · page N", as the
 * assistant's card says it, plus what tells it apart from another countertop on the same page. The
 * API's whole label stays in the title.
 */
export function CountertopTitle({ row, rows }: { row: Row; rows: Siblings }) {
  const extra = distinguishingLabel(row, rows);
  return (
    <span title={row.label}>
      Countertop · page <span className="num">{row.page_number}</span>
      {extra && <> · <Words text={extra} /></>}
    </span>
  );
}

/**
 * The same name where the page is already in its own column: "Countertop", plus what tells it apart.
 */
export function CountertopName({ row, rows }: { row: Row; rows: Siblings }) {
  const extra = distinguishingLabel(row, rows);
  return <>Countertop{extra && <> · <Words text={extra} /></>}</>;
}

/**
 * A countertop a person confirmed (#1245), as the heading its results are grouped under:
 * "Countertop 2 · page 5". The API's whole label stays in the title.
 */
export function CountertopItemTitle({ item }: { item: CountertopItem }) {
  return (
    <span title={item.label}>
      Countertop <span className="num">{item.number}</span> · page <span className="num">{item.page_number}</span>
    </span>
  );
}
