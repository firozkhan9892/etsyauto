import { defineRailway, github, preserve, project, service } from "railway/iac";

export default defineRailway(() => {
  const etsyauto = service("etsyauto", {
    source: github("firozkhan9892/etsyauto", { checkSuites: false }),
    start: "python scheduler.py",
    replicas: { "us-west2": 1 },
    env: { ETSY_API_KEY: preserve(), ETSY_KEYSTRING: preserve(), ETSY_SHARED_SECRET: preserve(), ETSY_SHOP_ID: preserve(), GROQ_API_KEY: preserve(), NVIDIA_API_KEY: preserve() },
  });

  return project("empathetic-upliftment", {
    resources: [etsyauto],
  });
});
