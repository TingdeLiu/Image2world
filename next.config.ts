import type { NextConfig } from "next";
import path from "path";

const nextConfig: NextConfig = {
  transpilePackages: ['three', 'postprocessing', '@react-three/fiber', '@react-three/drei'],
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
