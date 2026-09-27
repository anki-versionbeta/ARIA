import {
  Alert,
  Button,
  Card,
  CardBody,
  Field,
  H1,
  Logo,
  P,
  TextInput,
} from "@abbvie-unity/react";
import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useState } from "react";
import { ApiError } from "@/api/http";
import { useLogin } from "@/api/queries";

export const Route = createFileRoute("/login")({
  component: LoginPage,
});

function errorMessage(error: unknown) {
  if (error instanceof ApiError && error.status === 401) {
    return "Incorrect username or password.";
  }
  return "Sign in is unavailable right now. Please try again.";
}

function LoginPage() {
  const navigate = useNavigate();
  const login = useLogin();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate(
      { username, password },
      { onSuccess: () => navigate({ to: "/", replace: true }) }
    );
  };

  return (
    <div className="flex min-h-dvh items-center justify-center p-4">
      <Card className="w-full max-w-[420px]">
        <CardBody>
          <form onSubmit={handleSubmit} className="flex flex-col gap-6">
            <div className="flex flex-col gap-2">
              <Logo kind="circle" />
              <H1 styledAs="h3">ARIA</H1>
              <P className="text-muted">
                AI-Driven Report Intelligence &amp; Automation
              </P>
              <P className="text-muted">Sign in with your AbbVie account.</P>
            </div>

            {login.isError ? (
              <Alert status="error">{errorMessage(login.error)}</Alert>
            ) : null}

            <Field label="Username" floatingLabel block>
              <TextInput
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                autoFocus
                required
                className="min-w-0"
              />
            </Field>

            <Field label="Password" floatingLabel block>
              <TextInput
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
                className="min-w-0"
              />
            </Field>

            <Button
              type="submit"
              disabled={login.isPending || !username || !password}
            >
              {login.isPending ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        </CardBody>
      </Card>
    </div>
  );
}
