import { redirect } from 'next/navigation'
import { AUTH_ENABLED } from '../../utils/auth'
import { SignInForm } from '../../components/SignInForm'

export const dynamic = 'force-dynamic'

interface PageProps {
  searchParams: Promise<{ next?: string }>
}

export default async function SignInPage({ searchParams }: PageProps) {
  // Nothing to sign into on an instance with no password configured, and a form
  // that can only ever be refused is worse than no form.
  if (!AUTH_ENABLED) redirect('/')

  const { next } = await searchParams
  return <SignInForm next={next} />
}
