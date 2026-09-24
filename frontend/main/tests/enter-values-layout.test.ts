import assert from 'node:assert/strict';

import { layoutChoiceDefaults } from '../src/pages/layoutChoices.js';

const crop = {
  crop_artifact_id: 'crop-1',
  model_id: 'amazon.nova-lite-v1:0',
  prompt_id: 'layout-discriminator-v1',
  confirmed: false,
};

assert.deepEqual(
  layoutChoiceDefaults([
    {
      name: 'wall_config',
      choices: ['back_only', 'back_left_right'],
      proposal: { ...crop, value: 'back_only' },
    },
    {
      name: 'filler_symmetry',
      choices: ['equal_unless_noted', 'reviewer_noted_asymmetric'],
      proposal: null,
    },
    {
      name: 'material',
      choices: ['quartz'],
      proposal: { ...crop, value: 'granite' },
    },
  ]),
  { wall_config: 'back_only' },
);

console.log('enter-values layout defaults test passed');
