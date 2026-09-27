/**
 * A real `POST /filler-distribution` response, captured 2026-09-27.
 *
 * Not a fixture: this is what the running server sent, against a real database, for Raj's slide-6
 * example — a 90" wall measured at 82", 3" fillers, and a 36" equipment cabinet between two 24"
 * regulars. Captured rather than written, because the thing it is here to catch is the shape the
 * server actually sends differing from the shape the component reads, and a hand-written object
 * cannot catch that.
 *
 * Recapture with the request in `tests/live-elevation.test.tsx`'s header if the endpoint changes.
 */
import type { FillerDistributionResponse } from '../src/components/measure/fillerDistribution.js';

export const LIVE_DISTRIBUTION = {
  outcome: 'PASS',
  condition: 'cabinets_absorb_remainder',
  message:
    'Wall to wall width in the architectural drawing = 90". Wall to wall width as per site dimensions = 82". So 8" needs to be reduced in the shop drawing cabinet elevation. Since the minimum width of the filler is 2" per side, the fillers can only be reduced from 3" to 2" on each side. That absorbs 2" in total, so 6" still needs to be adjusted in the cabinets. The equipment cabinet width cannot be less than 36", so the full 6" is taken by the other cabinets. They are reduced from 24" to 21" each.',
  summary:
    'The fillers reached their limit, so the rest is divided equally between the regular cabinets. The equipment cabinets keep their width.',
  design_width: {
    numerator: '90',
    denominator: '1',
    unit: 'in',
    display: '90"',
    as_typed: null,
  },
  site_difference: {
    numerator: '-8',
    denominator: '1',
    unit: 'in',
    display: '-8"',
    as_typed: null,
  },
  field_dimension: {
    name: 'field_width',
    source: 'USER_INPUT',
    status: 'HUMAN_CONFIRMED',
    value: {
      numerator: '82',
      denominator: '1',
      unit: 'in',
      display: '82"',
      as_typed: '82"',
    },
  },
  fillers: [
    {
      id: 'F-L',
      original: {
        numerator: '3',
        denominator: '1',
        unit: 'in',
        display: '3"',
        as_typed: '3"',
      },
      proposed: {
        numerator: '2',
        denominator: '1',
        unit: 'in',
        display: '2"',
        as_typed: null,
      },
    },
    {
      id: 'F-R',
      original: {
        numerator: '3',
        denominator: '1',
        unit: 'in',
        display: '3"',
        as_typed: '3"',
      },
      proposed: {
        numerator: '2',
        denominator: '1',
        unit: 'in',
        display: '2"',
        as_typed: null,
      },
    },
  ],
  cabinets: [
    {
      id: 'CAB-1',
      type: 'double_door',
      original: {
        numerator: '24',
        denominator: '1',
        unit: 'in',
        display: '24"',
        as_typed: '24"',
      },
      proposed: {
        numerator: '21',
        denominator: '1',
        unit: 'in',
        display: '21"',
        as_typed: null,
      },
      adjustable: true,
    },
    {
      id: 'CAB-EQUIP',
      type: 'equipment',
      original: {
        numerator: '36',
        denominator: '1',
        unit: 'in',
        display: '36"',
        as_typed: '36"',
      },
      proposed: {
        numerator: '36',
        denominator: '1',
        unit: 'in',
        display: '36"',
        as_typed: '36"',
      },
      adjustable: false,
    },
    {
      id: 'CAB-2',
      type: 'double_door',
      original: {
        numerator: '24',
        denominator: '1',
        unit: 'in',
        display: '24"',
        as_typed: '24"',
      },
      proposed: {
        numerator: '21',
        denominator: '1',
        unit: 'in',
        display: '21"',
        as_typed: null,
      },
      adjustable: true,
    },
  ],
  cabinets_retained: false,
  reviewer_action: null,
  operands: [
    {
      name: 'field_width',
      source: 'USER_INPUT',
      status: 'HUMAN_CONFIRMED',
      value: {
        numerator: '82',
        denominator: '1',
        unit: 'in',
        display: '82"',
        as_typed: '82"',
      },
    },
    {
      name: 'design_width',
      source: 'ARCH',
      status: 'HUMAN_CONFIRMED',
      value: {
        numerator: '90',
        denominator: '1',
        unit: 'in',
        display: '90"',
        as_typed: null,
      },
    },
    {
      name: 'design_fillers',
      source: 'ARCH',
      status: 'HUMAN_CONFIRMED',
      value: {
        numerator: '6',
        denominator: '1',
        unit: 'in',
        display: '6"',
        as_typed: null,
      },
    },
    {
      name: 'filler_bounds',
      source: 'LITERAL',
      status: 'HUMAN_CONFIRMED',
      value: {
        numerator: '2',
        denominator: '1',
        unit: 'in',
        display: '2"',
        as_typed: '2"',
      },
    },
  ],
  calculation:
    'shop drawing does not match the exact distribution: fillers to 4 in total, then -3 in to each of 2 regular cabinet(s)',
} as unknown as FillerDistributionResponse;
