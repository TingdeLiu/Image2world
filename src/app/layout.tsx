import type { Metadata, Viewport } from 'next'
import { Theme } from '@radix-ui/themes'
import '@radix-ui/themes/styles.css'
import './globals.css'

export const metadata: Metadata = {
  title: {
    default: 'Image2World — Step Inside Your Image',
    template: '%s · Image2World',
  },
  description: 'Turn one image into a navigable 3D world — a Gaussian splat with a collision mesh you can walk through, generated on your own GPU or in the cloud.',
  applicationName: 'Image2World',
  keywords: [
    'image to 3D',
    'AI 3D world generator',
    'Gaussian splatting',
    '3D scene editor',
    'local AI',
  ],
  robots: { index: true, follow: true },
  openGraph: {
    type: 'website',
    title: 'Image2World — Step Inside Your Image',
    description: 'Turn one image into an explorable, editable 3D world.',
    siteName: 'Image2World',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Image2World — Step Inside Your Image',
    description: 'Turn one image into an explorable, editable 3D world.',
  },
}

export const viewport: Viewport = {
  themeColor: '#0a0b0a',
  colorScheme: 'dark',
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-black text-white">
        <Theme appearance="dark" hasBackground={false}>
          {children}
        </Theme>
      </body>
    </html>
  )
}
