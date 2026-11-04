"use strict";

/* One catalogue for the two browser approximations of the graded render. */
(function (root) {
  root.BriefcutLookStyles = {
    preview: {
      none: [],
      auto: [["contrast", 1.05], ["saturate", 1.06], ["brightness", 1.02]],
      warm: [["sepia", 0.16], ["saturate", 1.1], ["contrast", 1.02]],
      cool: [["saturate", 0.94], ["hue-rotate", 8], ["contrast", 1.03], ["brightness", 1.01]],
      vivid: [["saturate", 1.32], ["contrast", 1.06]],
      cinematic: [["contrast", 1.08], ["saturate", 0.9], ["sepia", 0.08], ["hue-rotate", -6]],
      film: [["contrast", 0.92], ["brightness", 1.03], ["saturate", 0.9], ["sepia", 0.1]],
      mono: [["grayscale", 1], ["contrast", 1.04]],
      noir: [["grayscale", 1], ["contrast", 1.36], ["brightness", 0.92]],
      vintage: [["sepia", 0.3], ["saturate", 0.82], ["contrast", 0.92], ["brightness", 1.04]],
    },
    previewVignette: { cinematic: 0.26, film: 0.1, noir: 0.42 },
    inspector: {
      auto: { contrast: 1.06, saturate: 1.08, brightness: 1.02 },
      warm: { sepia: 0.2, saturate: 1.14, brightness: 1.02, tint: ["#ff9a3d", 0.14] },
      cool: { saturate: 0.95, brightness: 1.01, tint: ["#2f7bff", 0.24] },
      vivid: { saturate: 1.42, contrast: 1.1 },
      cinematic: { contrast: 1.12, saturate: 0.92, sepia: 0.06, tint: ["#0f7f8c", 0.22] },
      film: { contrast: 0.92, brightness: 1.05, saturate: 0.9, sepia: 0.1 },
      mono: { grayscale: 1, contrast: 1.04 },
      noir: { grayscale: 1, contrast: 1.42, brightness: 0.9 },
      vintage: { sepia: 0.34, saturate: 0.82, contrast: 0.92, brightness: 1.05 },
    },
  };
})(window);
