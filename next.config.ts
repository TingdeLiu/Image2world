import type { NextConfig } from "next";
import path from "path";

const nextConfig: NextConfig = {
  transpilePackages: ['three', 'postprocessing', '@react-three/fiber', '@react-three/drei'],
  // `readWorlds()` discovers worlds by reading `public/worlds` at request time.
  // That path is built at runtime, so Next's static tracing cannot see it and a
  // serverless deployment would ship a lambda with no worlds in it -- the CDN
  // serves the assets, but the scanner that lists them finds an empty directory
  // and every page 404s. Declaring the directory keeps the scanner working off
  // a real filesystem when deployed.
  outputFileTracingIncludes: {
    '/**': ['./public/worlds/**'],
  },
  turbopack: {
    resolveAlias: {
      'three': './src/utils/three-compat.ts',
    },
  },
  webpack: (config) => {
    config.resolve.alias = {
      ...config.resolve.alias,
      'three$': path.resolve(process.cwd(), 'src/utils/three-compat.ts'),
    };
    return config;
  },
};

export default nextConfig;
