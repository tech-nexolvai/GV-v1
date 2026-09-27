/**
 * The cabinet run drawn twice: as the architectural drawing has it, and as corrected for the site.
 *
 * This is Raj's slide 6 and slide 10 as a drawing rather than a paragraph. A reviewer should see
 * what moved without reading anything: changed parts carry the review colour and an ✕, parts kept
 * from the arch drawing carry a ✓, and the equipment cabinet is hatched because its width is fixed.
 *
 * **Nothing here is written by a model.** Every box and every figure comes from the distribution
 * response, which is the verdict service's arithmetic. See `elevation.ts` for how the geometry is
 * derived; this file only draws it.
 */

import { useId } from 'react';
import type { FillerDistributionResponse } from '../measure/fillerDistribution';
import {
  buildElevation,
  monoLabelWidth,
  placeSegments,
  type ElevationLayout,
  type Segment,
} from './elevation.js';

const VIEW_WIDTH = 760;
const MARGIN_X = 44;
const WALL = 12;
const STRIP_HEIGHT = 44;
const ROW_HEIGHT = 150;

interface ElevationDiagramProps {
  result: FillerDistributionResponse;
  /**
   * Whether to print the outcome banner for an unresolved run. A host that already states the
   * outcome (the calculator panel does) passes false, so the reviewer does not read it twice.
   */
  showBanner?: boolean;
}

export function ElevationDiagram({ result, showBanner = true }: ElevationDiagramProps) {
  const uid = useId().replace(/:/g, '');
  const layout = buildElevation(result);
  // Nothing to draw: an abstention can return no parts at all. An empty frame would read as a
  // broken drawing, so the host's own message stands alone.
  if (layout.elements.length === 0) return null;
  const usable = VIEW_WIDTH - MARGIN_X * 2;
  const longest = Math.max(layout.archTotal.value, layout.siteTotal?.value ?? 0);
  const pxPerUnit = longest > 0 ? usable / longest : 0;

  const archSegments = placeSegments(layout.elements, 'original', MARGIN_X, pxPerUnit);
  const siteSegments = layout.hasProposal
    ? placeSegments(layout.elements, 'proposed', MARGIN_X, pxPerUnit)
    : [];

  const height = layout.hasProposal ? ROW_HEIGHT * 2 + 8 : ROW_HEIGHT + 8;
  const changedCount = layout.elements.filter((element) => element.changed).length;
  const difference = result.site_difference?.display ?? null;

  const hatch = `elev-hatch-${uid}`;
  const wallHatch = `elev-wall-${uid}`;
  const titleId = `elev-title-${uid}`;
  const descId = `elev-desc-${uid}`;

  return (
    <figure className="elevation" data-outcome={result.outcome}>
      {!layout.hasProposal && showBanner && <OutcomeBanner result={result} />}

      <svg
        className="elevation__svg"
        viewBox={`0 0 ${VIEW_WIDTH} ${height}`}
        role="img"
        aria-labelledby={`${titleId} ${descId}`}
      >
        <title id={titleId}>Cabinet elevation</title>
        <desc id={descId}>{describe(layout, changedCount)}</desc>

        <defs>
          <pattern id={hatch} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="6" className="elevation__hatch-line" />
          </pattern>
          <pattern id={wallHatch} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
            <line x1="0" y1="0" x2="0" y2="5" className="elevation__wall-line" />
          </pattern>
        </defs>

        <Row
          top={0}
          role="arch"
          title="Arch drawing"
          total={layout.archTotal.display}
          segments={archSegments}
          hatch={hatch}
          wallHatch={wallHatch}
          corrected={false}
        />

        {layout.hasProposal && layout.siteTotal && (
          <Row
            top={ROW_HEIGHT + 8}
            role="site"
            title="Corrected for site"
            badge={difference}
            total={layout.siteTotal.display}
            segments={siteSegments}
            hatch={hatch}
            wallHatch={wallHatch}
            corrected
          />
        )}
      </svg>

      <figcaption className="elevation__legend">
        {layout.hasProposal && (
          <>
            <span className="elevation__key">
              <span className="elevation__mark elevation__mark--changed" aria-hidden="true">✕</span>
              Corrected
            </span>
            <span className="elevation__key">
              <span className="elevation__mark elevation__mark--kept" aria-hidden="true">✓</span>
              Kept from arch
            </span>
          </>
        )}
        <span className="elevation__key">
          <span className="elevation__swatch elevation__swatch--fixed" aria-hidden="true" />
          Equipment, width fixed
        </span>
        <span className="elevation__key elevation__key--note">Drawn to scale</span>
        {!layout.sideKnown && (
          <span className="elevation__key elevation__key--note">Filler side not recorded</span>
        )}
      </figcaption>
    </figure>
  );
}

interface RowProps {
  top: number;
  role: 'arch' | 'site';
  title: string;
  badge?: string | null;
  total: string;
  segments: Segment[];
  hatch: string;
  wallHatch: string;
  corrected: boolean;
}

function Row({ top, role, title, badge, total, segments, hatch, wallHatch, corrected }: RowProps) {
  if (segments.length === 0) return null;
  const start = segments[0].x;
  const end = segments[segments.length - 1].x + segments[segments.length - 1].width;
  const titleY = top + 16;
  // 22 units between the dimension line and the strip, so the total's label never meets the
  // ✕ / ✓ marks that sit on the strip's top edge.
  const dimY = top + 36;
  const stripY = top + 58;
  const stripBottom = stripY + STRIP_HEIGHT;

  return (
    <g className={`elevation__row elevation__row--${role}`}>
      {/* Row title, with the role colour the findings table uses for its columns. */}
      <circle cx={start + 4} cy={titleY - 4} r={4} className={`elevation__role-dot elevation__role-dot--${role}`} />
      <text x={start + 14} y={titleY} className="elevation__title">
        {title}
        {badge && (
          <tspan dx={8} className="elevation__badge">
            {badge.startsWith('-') || badge.startsWith('−') ? badge : `+${badge}`}
          </tspan>
        )}
      </text>

      {/* Overall wall-to-wall dimension line. */}
      <line x1={start} y1={dimY} x2={end} y2={dimY} className="elevation__dim-line" />
      <line x1={start} y1={dimY - 5} x2={start} y2={dimY + 5} className="elevation__dim-line" />
      <line x1={end} y1={dimY - 5} x2={end} y2={dimY + 5} className="elevation__dim-line" />
      <rect
        x={(start + end) / 2 - monoLabelWidth(total, 13) / 2 - 6}
        y={dimY - 9}
        width={monoLabelWidth(total, 13) + 12}
        height={17}
        rx={3}
        className="elevation__dim-bg"
      />
      <text x={(start + end) / 2} y={dimY + 4} className="elevation__dim-total" textAnchor="middle">
        {total}
      </text>

      {/* Walls. */}
      <rect x={start - WALL} y={stripY - 8} width={WALL} height={STRIP_HEIGHT + 16} fill={`url(#${wallHatch})`} className="elevation__wall" />
      <rect x={end} y={stripY - 8} width={WALL} height={STRIP_HEIGHT + 16} fill={`url(#${wallHatch})`} className="elevation__wall" />

      {segments.map((segment) => (
        <SegmentBox
          key={segment.element.id}
          segment={segment}
          stripY={stripY}
          stripBottom={stripBottom}
          hatch={hatch}
          corrected={corrected}
        />
      ))}
    </g>
  );
}

interface SegmentBoxProps {
  segment: Segment;
  stripY: number;
  stripBottom: number;
  hatch: string;
  corrected: boolean;
}

function SegmentBox({ segment, stripY, stripBottom, hatch, corrected }: SegmentBoxProps) {
  const { element, x, width } = segment;
  const changed = corrected && element.changed;
  const centre = x + width / 2;
  const state = changed ? 'changed' : 'kept';

  const labelText = changed
    ? `${element.original.display} ${element.proposed.display}`
    : segment.display;
  const fits = monoLabelWidth(labelText) <= width - 6;
  const labelY = fits ? stripBottom + 18 : stripBottom + 38;

  return (
    <g className={`elevation__segment elevation__segment--${element.kind}`} data-state={corrected ? state : 'arch'}>
      {/* One string child: React 19 renders <title> only from a single string. */}
      <title>{tooltip(element, changed, segment.display)}</title>

      <rect x={x} y={stripY} width={width} height={STRIP_HEIGHT} className="elevation__box" />
      {element.kind === 'equipment' && (
        <rect x={x} y={stripY} width={width} height={STRIP_HEIGHT} fill={`url(#${hatch})`} className="elevation__fixed" />
      )}

      {width >= 24 && (
        <text x={centre} y={stripY + STRIP_HEIGHT / 2 + 4} className="elevation__code" textAnchor="middle">
          {element.code}
        </text>
      )}

      {/* Boundary ticks form the dimension chain under the strip. */}
      <line x1={x} y1={stripBottom} x2={x} y2={stripBottom + 6} className="elevation__tick" />
      <line x1={x + width} y1={stripBottom} x2={x + width} y2={stripBottom + 6} className="elevation__tick" />

      {!fits && (
        <line x1={centre} y1={stripBottom + 6} x2={centre} y2={labelY - 11} className="elevation__leader" />
      )}

      <text x={centre} y={labelY} className="elevation__dim" textAnchor="middle">
        {changed ? (
          <>
            <tspan className="elevation__dim-old">{element.original.display}</tspan>
            <tspan dx={4} className="elevation__dim-new">{element.proposed.display}</tspan>
          </>
        ) : (
          segment.display
        )}
      </text>

      {corrected && (
        <g className={`elevation__badge-mark elevation__badge-mark--${state}`}>
          <circle cx={centre} cy={stripY} r={8} />
          <text x={centre} y={stripY + 4} textAnchor="middle">{changed ? '✕' : '✓'}</text>
        </g>
      )}
    </g>
  );
}

function tooltip(element: Segment['element'], changed: boolean, display: string): string {
  return changed
    ? `${element.name}: ${element.original.display} on the arch drawing, ${element.proposed.display} corrected`
    : `${element.name}: ${display}`;
}

function OutcomeBanner({ result }: { result: FillerDistributionResponse }) {
  const title = result.outcome === 'REVIEW_REQUIRED'
    ? 'Cannot fit. RFI to architect.'
    : 'Missing input. No correction drawn.';
  return (
    <div className="elevation__banner" data-outcome={result.outcome} role="status">
      <strong>{title}</strong>
      {result.message && <span>{result.message}</span>}
    </div>
  );
}

function describe(layout: ElevationLayout, changedCount: number): string {
  const parts = layout.elements.map((element) => `${element.name} ${element.original.display}`).join(', ');
  if (!layout.hasProposal) return `Arch drawing, ${layout.archTotal.display}: ${parts}. No correction drawn.`;
  return (
    `Arch drawing ${layout.archTotal.display}, corrected for site ${layout.siteTotal?.display}. ` +
    `${changedCount} part${changedCount === 1 ? '' : 's'} corrected.`
  );
}
