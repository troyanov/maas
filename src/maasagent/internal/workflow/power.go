package workflow

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"reflect"
	"strings"
	"time"

	"go.temporal.io/sdk/activity"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/workflow"
	"maas.io/core/src/maasagent/internal/workflow/log/tag"
)

const (
	// Maximum power activity duration (to cope with broken BMCs)
	powerActivityDuration = 5 * time.Minute
)

var (
	// ErrWrongPowerState is an error for when a power action executes
	// and the machine is found in an incorrect power state
	ErrWrongPowerState = errors.New("BMC is in the wrong power state")
)

// PowerParam is the workflow parameter for power management of a host
type PowerParam struct {
	SystemID   string                 `json:"system_id"`
	TaskQueue  string                 `json:"task_queue"`
	DriverOpts map[string]interface{} `json:"driver_opts"`
	DriverType string                 `json:"driver_type"`
}

// powerCLIExecutableName returns correct MAAS Power CLI executable name
// depending on the installation type (snap or deb package)
func powerCLIExecutableName() string {
	if os.Getenv("SNAP") == "" {
		return "maas.power"
	}

	return "maas-power"
}

func fmtPowerOpts(opts map[string]interface{}) []string {
	var res []string

	for k, v := range opts {
		// skip 'system_id' as it is not required by any power driver contract.
		// it is added by the region when driver is called directly (not via CLI)
		// also skip 'null' values (some power options might have them empty)
		if k == "system_id" || v == nil {
			continue
		}

		k = strings.ReplaceAll(k, "_", "-")

		switch reflect.TypeOf(v).Kind() {
		case reflect.Slice:
			s := reflect.ValueOf(v)
			for i := 0; i < s.Len(); i++ {
				vStr := fmt.Sprintf("%v", s.Index(i))

				res = append(res, fmt.Sprintf("--%s", k), vStr)
			}
		default:
			vStr := fmt.Sprintf("%v", v)
			if len(vStr) == 0 {
				continue
			}

			res = append(res, fmt.Sprintf("--%s", k), vStr)
		}
	}

	return res
}

// PowerActivityParam is the activity parameter for PowerActivity
type PowerActivityParam struct {
	Action string `json:"action"`
	PowerParam
}

// PowerResult is the result of power actions
type PowerResult struct {
	State string `json:"state"`
}

// PowerActivity executes power actions via the maas.power CLI
func PowerActivity(ctx context.Context, params PowerActivityParam) (*PowerResult, error) {
	log := activity.GetLogger(ctx)

	maasPowerCLI, err := exec.LookPath(powerCLIExecutableName())

	if err != nil {
		log.Error("MAAS power CLI executable path lookup failure",
			tag.Builder().Error(err).KeyVals...)
		return nil, err
	}

	opts := fmtPowerOpts(params.DriverOpts)
	args := append([]string{params.Action, params.DriverType}, opts...)

	log.Debug("Executing MAAS power CLI", tag.Builder().KV("args", args).KeyVals...)

	//nolint:gosec // gosec's G204 flags any command execution using variables
	cmd := exec.CommandContext(ctx, maasPowerCLI, args...)

	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	err = cmd.Run()
	if err != nil {
		t := tag.Builder().Error(err)
		if stdout.String() != "" {
			t = t.KV("stdout", stdout.String())
		}

		if stderr.String() != "" {
			t = t.KV("stderr", stderr.String())
		}

		log.Error("Error executing power command", t.KeyVals...)

		return nil, err
	}

	res := &PowerResult{
		State: strings.TrimSpace(stdout.String()),
	}

	return res, nil
}

func execPowerActivity(ctx workflow.Context, params PowerActivityParam) workflow.Future {
	ctx = workflow.WithActivityOptions(ctx, workflow.ActivityOptions{
		StartToCloseTimeout: powerActivityDuration,
		RetryPolicy: &temporal.RetryPolicy{
			MaximumAttempts: 5,
		},
	})

	return workflow.ExecuteActivity(ctx, "power", params)
}

// PowerOn will power on a host
func PowerOn(ctx workflow.Context, params PowerParam) (*PowerResult, error) {
	log := workflow.GetLogger(ctx)

	log.Info("Powering on", tag.Builder().TargetSystemID(params.SystemID).KeyVals...)

	activityParams := PowerActivityParam{
		Action:     "on",
		PowerParam: params,
	}

	var res PowerResult

	err := execPowerActivity(ctx, activityParams).Get(ctx, &res)
	if err != nil {
		return nil, err
	}

	if res.State != "on" {
		return nil, ErrWrongPowerState
	}

	return &res, nil
}

// PowerOff will power off a host
func PowerOff(ctx workflow.Context, params PowerParam) (*PowerResult, error) {
	log := workflow.GetLogger(ctx)

	log.Info("Powering off", tag.Builder().TargetSystemID(params.SystemID).KeyVals...)

	activityParams := PowerActivityParam{
		Action:     "off",
		PowerParam: params,
	}

	var res PowerResult

	err := execPowerActivity(ctx, activityParams).Get(ctx, &res)
	if err != nil {
		return nil, err
	}

	if res.State != "off" {
		return nil, ErrWrongPowerState
	}

	return &res, nil
}

// PowerCycle will power cycle a host
func PowerCycle(ctx workflow.Context, params PowerParam) (*PowerResult, error) {
	log := workflow.GetLogger(ctx)

	log.Info("Cycling power", tag.Builder().TargetSystemID(params.SystemID).KeyVals...)

	activityParams := PowerActivityParam{
		Action:     "cycle",
		PowerParam: params,
	}

	var res PowerResult

	err := execPowerActivity(ctx, activityParams).Get(ctx, &res)
	if err != nil {
		return nil, err
	}

	if res.State != "on" {
		return nil, ErrWrongPowerState
	}

	return &res, nil
}

// PowerQuery will query the power state of a host
func PowerQuery(ctx workflow.Context, params PowerParam) (*PowerResult, error) {
	log := workflow.GetLogger(ctx)

	log.Info("Querying power status", tag.Builder().TargetSystemID(params.SystemID).KeyVals...)

	activityParams := PowerActivityParam{
		Action:     "status",
		PowerParam: params,
	}

	var res PowerResult

	err := execPowerActivity(ctx, activityParams).Get(ctx, &res)
	if err != nil {
		return nil, err
	}

	return &res, nil
}
