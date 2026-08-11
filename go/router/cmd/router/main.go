// lake-router:P3 OpenAI 兼容 HTTP 入口。
//
//	go run ./router/cmd/router
//
// 环境变量:LAKE_HTTP_ADDR / LAKE_WORKER_ADDR / LAKE_AGENT_ADDR / LAKE_CP_ADDR /
// LAKE_NODE_ROLE / LAKE_MAX_INFLIGHT(P6.4 并发执行上限,默认 1;队列无界不拒请求)/
// LAKE_AUTOSCALE(P6.5 弹性扩缩开关,=1 开启;默认关——单进程原型不起真实 worker)
package main

import (
	"context"
	"errors"
	"log"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/chengda-wu/lake/go/router"
)

func main() {
	cfg := router.Config{
		HTTPAddr:    env("LAKE_HTTP_ADDR", ":8080"),
		WorkerAddr:  env("LAKE_WORKER_ADDR", "127.0.0.1:50053"),
		AgentAddr:   env("LAKE_AGENT_ADDR", "127.0.0.1:50054"),
		CPAddr:      env("LAKE_CP_ADDR", "127.0.0.1:50051"),
		NodeRole:    env("LAKE_NODE_ROLE", "hybrid"),
		MaxInFlight: envInt("LAKE_MAX_INFLIGHT", 1),
		Autoscale:   os.Getenv("LAKE_AUTOSCALE") == "1",
	}
	s, err := router.New(cfg)
	if err != nil {
		log.Fatal(err)
	}
	defer func() {
		if err := s.Close(); err != nil {
			slog.Error("close router failed", "err", err)
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
		slog.Info("lake-router OpenAI HTTP listening",
			"http_addr", cfg.HTTPAddr, "agent_addr", cfg.AgentAddr, "worker_addr", cfg.WorkerAddr)
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

func envInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}
