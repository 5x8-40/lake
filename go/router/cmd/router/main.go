// lake-router:P3 OpenAI 兼容 HTTP 入口。
//
//	go run ./router/cmd/router
//
// 环境变量:LAKE_HTTP_ADDR / LAKE_WORKER_ADDR / LAKE_AGENT_ADDR
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/chengda-wu/lake/go/router"
)

func main() {
	cfg := router.Config{
		HTTPAddr:   env("LAKE_HTTP_ADDR", ":8080"),
		WorkerAddr: env("LAKE_WORKER_ADDR", "127.0.0.1:50053"),
		AgentAddr:  env("LAKE_AGENT_ADDR", "127.0.0.1:50054"),
	}
	s, err := router.New(cfg)
	if err != nil {
		log.Fatal(err)
	}
	defer func() {
		if err := s.Close(); err != nil {
			log.Printf("close router: %v", err)
		}
	}()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	httpServer := &http.Server{
		Addr:    cfg.HTTPAddr,
		Handler: s.Handler(),
	}
	errCh := make(chan error, 1)
	go func() {
		log.Printf("lake-router OpenAI HTTP on %s → agent %s → worker %s",
			cfg.HTTPAddr, cfg.AgentAddr, cfg.WorkerAddr)
		errCh <- httpServer.ListenAndServe()
	}()

	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(shutdownCtx); err != nil {
			log.Fatal(err)
		}
	case err := <-errCh:
		if !errors.Is(err, http.ErrServerClosed) {
			log.Fatal(err)
		}
	}
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
